"""Download one IRS Form 990 XML filing into a chosen directory.

The default source is the public GivingTuesday Data Commons S3 data lake.
Pass a bare object ID, an XML filename, or a full HTTPS URL.
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


GT_XML_BASE_URL = (
    "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles"
)
OBJECT_ID_RE = re.compile(r"^(?:OID-)?(?P<object_id>\d{18})$")
XML_FILENAME_RE = re.compile(
    r"^(?P<object_id>\d{18})(?:_(?:public|private))?\.xml$",
    re.IGNORECASE,
)


class DownloadError(RuntimeError):
    """Raised when a filing cannot be downloaded or validated."""


def compatible_tls_context() -> ssl.SSLContext:
    """Build a verified TLS context compatible with Windows-managed CA chains."""
    context = ssl.create_default_context()
    # Python 3.13+ enables OpenSSL's strict X.509 checks by default. Some
    # Windows-managed CA chains are otherwise valid but omit the `critical`
    # marker on Basic Constraints. Clear only strict mode; CA and hostname
    # verification remain enabled.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def resolve_source(source: str) -> tuple[str, str]:
    """Return ``(download_url, safe_filename)`` for a CLI source value."""
    source = source.strip()
    object_match = OBJECT_ID_RE.fullmatch(source)
    if object_match:
        filename = f"{object_match.group('object_id')}_public.xml"
        return f"{GT_XML_BASE_URL}/{filename}", filename

    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise DownloadError("Full URLs must use HTTPS.")
        filename = Path(urllib.parse.unquote(parsed.path)).name
        if not XML_FILENAME_RE.fullmatch(filename):
            raise DownloadError(
                "The URL must end in an 18-digit IRS object ID XML filename."
            )
        return source, filename

    filename = Path(source).name
    filename_match = XML_FILENAME_RE.fullmatch(filename)
    if not filename_match or filename != source:
        raise DownloadError(
            "Source must be an 18-digit object ID, an IRS XML filename, "
            "or a full HTTPS URL."
        )

    # The GivingTuesday data lake uses the public suffix even if callers pass
    # only the object ID plus .xml.
    object_id = filename_match.group("object_id")
    canonical_filename = f"{object_id}_public.xml"
    return f"{GT_XML_BASE_URL}/{canonical_filename}", canonical_filename


def validate_return_xml(content: bytes) -> None:
    """Reject empty, malformed, or non-IRS-return responses."""
    if not content:
        raise DownloadError("The server returned an empty response.")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise DownloadError(f"The response is not valid XML: {exc}") from exc

    root_name = root.tag.rsplit("}", 1)[-1]
    if root_name != "Return":
        raise DownloadError(
            f"Expected an IRS Return XML document; found root element {root_name!r}."
        )

    header_names = {
        element.tag.rsplit("}", 1)[-1]
        for element in root.iter()
    }
    required = {"ReturnHeader", "EIN", "TaxYr"}
    missing = sorted(required - header_names)
    if missing:
        raise DownloadError(
            "The XML does not contain required IRS return fields: " + ", ".join(missing)
        )


def download_xml(
    source: str,
    destination: Path,
    *,
    overwrite: bool = False,
    timeout: float = 60.0,
) -> Path:
    """Download and atomically store one validated filing."""
    url, filename = resolve_source(source)
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output_path = destination / filename

    if output_path.exists() and not overwrite:
        raise DownloadError(
            f"Destination already exists: {output_path}. Use --overwrite to replace it."
        )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "irs990-tool/1.0", "Accept": "application/xml,text/xml"},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=compatible_tls_context(),
        ) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"Download failed with HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"Download failed: {exc.reason}") from exc

    validate_return_xml(content)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{filename}.",
            suffix=".part",
            dir=destination,
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        temp_path.replace(output_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and validate one IRS Form 990 XML filing.",
    )
    parser.add_argument(
        "source",
        help="IRS object ID, XML filename, or full HTTPS URL",
    )
    parser.add_argument(
        "destination",
        type=Path,
        help="Directory in which to store the XML file",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing destination file",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Network timeout in seconds (default: 60)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0:
        print("Error: --timeout must be greater than zero.")
        return 2

    try:
        output_path = download_xml(
            args.source,
            args.destination,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
    except DownloadError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Downloaded and validated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
