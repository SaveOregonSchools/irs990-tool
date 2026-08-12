import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import download_irs990_xml as downloader


SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Return xmlns="http://www.irs.gov/efile">
  <ReturnHeader>
    <TaxYr>2016</TaxYr>
    <Filer><EIN>521198450</EIN></Filer>
  </ReturnHeader>
</Return>
"""


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.content


class DownloadIrs990XmlTests(unittest.TestCase):
    def test_resolve_source_accepts_object_id_filename_and_url(self):
        expected_name = "201821349349309507_public.xml"
        expected_url = f"{downloader.GT_XML_BASE_URL}/{expected_name}"

        self.assertEqual(
            downloader.resolve_source("201821349349309507"),
            (expected_url, expected_name),
        )
        self.assertEqual(
            downloader.resolve_source(expected_name),
            (expected_url, expected_name),
        )
        self.assertEqual(
            downloader.resolve_source(expected_url),
            (expected_url, expected_name),
        )

    def test_resolve_source_rejects_unsafe_or_non_https_input(self):
        for source in ("../filing.xml", "filing.xml", "http://example.com/123.xml"):
            with self.subTest(source=source), self.assertRaises(downloader.DownloadError):
                downloader.resolve_source(source)

    @patch("download_irs990_xml.urllib.request.urlopen")
    def test_download_validates_and_writes_atomically(self, urlopen):
        urlopen.return_value = FakeResponse(SAMPLE_XML)
        with tempfile.TemporaryDirectory() as tmp:
            output = downloader.download_xml("201821349349309507", Path(tmp))

            self.assertEqual(output.name, "201821349349309507_public.xml")
            self.assertEqual(output.read_bytes(), SAMPLE_XML)
            self.assertEqual(list(Path(tmp).glob("*.part")), [])
            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.full_url,
                f"{downloader.GT_XML_BASE_URL}/201821349349309507_public.xml",
            )
            self.assertIsInstance(urlopen.call_args.kwargs["context"], downloader.ssl.SSLContext)
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 60.0)

    @patch("download_irs990_xml.urllib.request.urlopen")
    def test_invalid_response_is_not_saved(self, urlopen):
        urlopen.return_value = FakeResponse(b"<html>not a filing</html>")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(downloader.DownloadError):
                downloader.download_xml("201821349349309507", Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    @patch("download_irs990_xml.urllib.request.urlopen")
    def test_existing_file_requires_overwrite(self, urlopen):
        urlopen.return_value = FakeResponse(SAMPLE_XML)
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "201821349349309507_public.xml"
            existing.write_bytes(b"existing")

            with self.assertRaises(downloader.DownloadError):
                downloader.download_xml("201821349349309507", Path(tmp))

            self.assertEqual(existing.read_bytes(), b"existing")
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
