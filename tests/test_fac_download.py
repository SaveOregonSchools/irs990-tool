import hashlib
import io
import json
import os
import ssl
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import build_fac_db
import fac_bulk


GENERAL_BYTES = (
    b"report_id,audit_year,auditee_ein,auditee_name\n"
    b"2024-12-GSAFAC-0000000001,2024,123456789,Fixture Charity\n"
)


class FakeResponse:
    def __init__(self, chunks, *, status=200, headers=None, url=None):
        self.chunks = list(chunks)
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.closed = False

    def read(self, _size=-1):
        if not self.chunks:
            return b""
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class QueueOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout, context):
        self.calls.append((request, timeout, context))
        response = self.responses.pop(0)
        if response.url is None:
            response.url = request.full_url
        return response


class MappingOpener:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, request, *, timeout, context):
        self.calls.append((request, timeout, context))
        body = self.payloads[request.full_url]
        return FakeResponse(
            [body],
            status=200,
            headers={"Content-Length": str(len(body)), "ETag": '"fixture"'},
            url=request.full_url,
        )


class FacDownloadTests(unittest.TestCase):
    def test_verified_tls_context_clears_only_strict_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "general.csv"
            opener = QueueOpener([
                FakeResponse(
                    [GENERAL_BYTES],
                    headers={
                        "Content-Length": str(len(GENERAL_BYTES)),
                        "ETag": '"one"',
                    },
                )
            ])
            result = fac_bulk.download_official_fac_file(
                fac_bulk.CURRENT_DOWNLOAD_URLS[0], destination, opener=opener
            )

            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(destination.read_bytes(), GENERAL_BYTES)
            context = opener.calls[0][2]
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
            if strict:
                self.assertEqual(context.verify_flags & strict, 0)
            self.assertFalse((Path(str(destination) + ".part")).exists())

    def test_nonallowlisted_url_is_rejected_before_transport(self):
        opener = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "allowlist"):
                fac_bulk.download_official_fac_file(
                    "https://example.com/general.csv",
                    Path(temp_dir) / "general.csv",
                    opener=opener,
                )
        opener.assert_not_called()

    def test_interrupted_stream_resumes_with_range_and_if_range(self):
        prefix_length = 17
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "general.csv"
            first = QueueOpener([
                FakeResponse(
                    [GENERAL_BYTES[:prefix_length], OSError("connection lost")],
                    headers={
                        "Content-Length": str(len(GENERAL_BYTES)),
                        "ETag": '"snapshot-a"',
                    },
                )
            ])
            with self.assertRaisesRegex(fac_bulk.FacDownloadError, "stream failed"):
                fac_bulk.download_official_fac_file(
                    fac_bulk.CURRENT_DOWNLOAD_URLS[0], destination, opener=first
                )

            part = Path(str(destination) + ".part")
            metadata = Path(str(destination) + ".part.json")
            self.assertEqual(part.read_bytes(), GENERAL_BYTES[:prefix_length])
            self.assertEqual(json.loads(metadata.read_text())["etag"], '"snapshot-a"')
            self.assertFalse(destination.exists())

            remainder = GENERAL_BYTES[prefix_length:]
            second = QueueOpener([
                FakeResponse(
                    [remainder],
                    status=206,
                    headers={
                        "Content-Length": str(len(remainder)),
                        "Content-Range": (
                            f"bytes {prefix_length}-{len(GENERAL_BYTES) - 1}/"
                            f"{len(GENERAL_BYTES)}"
                        ),
                        "ETag": '"snapshot-a"',
                    },
                )
            ])
            result = fac_bulk.download_official_fac_file(
                fac_bulk.CURRENT_DOWNLOAD_URLS[0], destination, opener=second
            )
            request = second.calls[0][0]
            self.assertEqual(request.get_header("Range"), f"bytes={prefix_length}-")
            self.assertEqual(request.get_header("If-range"), '"snapshot-a"')
            self.assertTrue(result["resumed"])
            self.assertEqual(destination.read_bytes(), GENERAL_BYTES)
            self.assertFalse(metadata.exists())

    def test_failed_refresh_preserves_finished_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "general.csv"
            original = b"last known good finished file"
            destination.write_bytes(original)
            invalid = b"<html>upstream error</html>"
            opener = QueueOpener([
                FakeResponse(
                    [invalid],
                    headers={"Content-Length": str(len(invalid)), "ETag": '"bad"'},
                )
            ])
            with self.assertRaisesRegex(fac_bulk.FacDownloadError, "unexpected header"):
                fac_bulk.download_official_fac_file(
                    fac_bulk.CURRENT_DOWNLOAD_URLS[0],
                    destination,
                    refresh=True,
                    opener=opener,
                )
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(Path(str(destination) + ".part").read_bytes(), invalid)

    def test_content_length_cap_prevents_body_write_and_preserves_finished(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "general.csv"
            destination.write_bytes(b"old")
            opener = QueueOpener([
                FakeResponse(
                    [GENERAL_BYTES],
                    headers={"Content-Length": str(len(GENERAL_BYTES))},
                )
            ])
            with self.assertRaisesRegex(fac_bulk.FacDownloadError, "size cap"):
                fac_bulk.download_official_fac_file(
                    fac_bulk.CURRENT_DOWNLOAD_URLS[0],
                    destination,
                    refresh=True,
                    max_bytes=len(GENERAL_BYTES) - 1,
                    opener=opener,
                )
            self.assertEqual(destination.read_bytes(), b"old")

    def test_historic_group_verifies_published_checksum_before_publish(self):
        archive_stream = io.BytesIO()
        with zipfile.ZipFile(archive_stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("2015/ELECAUDITHEADER.csv", "AUDITYEAR,DBKEY,EIN\n2015,1,123456789\n")
            archive.writestr("2015/ELECAUDITS.csv", "AUDITYEAR,DBKEY,AMOUNT\n2015,1,800000\n")
        archive_bytes = archive_stream.getvalue()
        checksum_bytes = (
            hashlib.sha1(archive_bytes).hexdigest() + "  census-1998-2015.zip\n"
        ).encode("ascii")
        opener = MappingOpener(
            {
                fac_bulk.HISTORIC_SHA1_URL: checksum_bytes,
                fac_bulk.HISTORIC_DOWNLOAD_URL: archive_bytes,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = fac_bulk.download_official_fac_sources(
                temp_dir, include_historic=True, opener=opener
            )
            historic = Path(result["historic_directory"])
            self.assertEqual(
                (historic / "census-1998-2015.zip").read_bytes(), archive_bytes
            )
            self.assertEqual(
                (historic / "census-1998-2015.sha1").read_bytes(), checksum_bytes
            )
            self.assertFalse(list(historic.glob("*.part")))

    def test_failed_historic_group_refresh_keeps_both_finished_files(self):
        old_archive_stream = io.BytesIO()
        with zipfile.ZipFile(old_archive_stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("2015/ELECAUDITHEADER.csv", "AUDITYEAR,DBKEY\n2015,1\n")
            archive.writestr("2015/ELECAUDITS.csv", "AUDITYEAR,DBKEY\n2015,1\n")
        old_archive = old_archive_stream.getvalue()
        old_checksum = (
            hashlib.sha1(old_archive).hexdigest() + "  census-1998-2015.zip\n"
        ).encode("ascii")

        new_expected_archive = b"the checksum expects different archive bytes"
        new_checksum = (
            hashlib.sha1(new_expected_archive).hexdigest()
            + "  census-1998-2015.zip\n"
        ).encode("ascii")
        bad_new_archive = b"not the archive named by the new checksum"
        opener = MappingOpener(
            {
                fac_bulk.HISTORIC_SHA1_URL: new_checksum,
                fac_bulk.HISTORIC_DOWNLOAD_URL: bad_new_archive,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            historic = Path(temp_dir) / "historic"
            historic.mkdir()
            checksum_path = historic / "census-1998-2015.sha1"
            archive_path = historic / "census-1998-2015.zip"
            checksum_path.write_bytes(old_checksum)
            archive_path.write_bytes(old_archive)

            with self.assertRaisesRegex(
                fac_bulk.FacDownloadError, "published SHA1"
            ):
                fac_bulk.download_official_fac_sources(
                    temp_dir,
                    include_historic=True,
                    refresh=True,
                    opener=opener,
                )

            self.assertEqual(checksum_path.read_bytes(), old_checksum)
            self.assertEqual(archive_path.read_bytes(), old_archive)
            self.assertEqual(
                Path(str(checksum_path) + ".part").read_bytes(), new_checksum
            )
            self.assertEqual(
                Path(str(archive_path) + ".part").read_bytes(), bad_new_archive
            )

    def test_cli_can_download_and_build_in_one_command(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            build_fac_db, "download_official_fac_sources"
        ) as downloader, mock.patch.object(
            build_fac_db, "build_fac_database"
        ) as builder, mock.patch("builtins.print"):
            root = Path(temp_dir)
            current = root / "current"
            downloader.return_value = {
                "current_directory": str(current),
                "historic_directory": None,
                "files": [],
            }
            builder.return_value = {"database": str(root / "fac.db")}
            exit_code = build_fac_db.main(
                [
                    "--download-current",
                    "--download-dir", str(root),
                    "--db", str(root / "fac.db"),
                    "--source-as-of", "2026-08-14",
                ]
            )
            self.assertEqual(exit_code, 0)
            downloader.assert_called_once()
            self.assertIn(current, builder.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
