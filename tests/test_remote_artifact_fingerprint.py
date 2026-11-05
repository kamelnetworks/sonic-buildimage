"""scripts/remote_artifact_fingerprint.sh gives the vendor SAI debs (platform/broadcom/sai-*.dep)
a cache-key flag that follows the remote blob, not the request: per-request headers (Date,
x-azure-ref, x-ms-request-id, ...) and HTTP/1.1 vs HTTP/2 header casing must not change it,
while a changed blob (etag / length / md5 / mtime) must."""
import os
import pathlib
import re
import subprocess
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "remote_artifact_fingerprint.sh"

BLOB = dict(length="64315416", md5="IDxnAGL32cVZmj/IUrKWgQ==",
            mtime="Wed, 30 Sep 2026 11:59:18 GMT", etag='"0x8DF1EEA41AD4F1A"')


def wget_output(http2, date, ref, reqid, **blob):
    """One `wget --spider --server-response` transcript as the dep files feed it to the script."""
    b = dict(BLOB, **blob)
    if http2:
        hdr = textwrap.dedent(f"""\
              HTTP/2 200
              date: {date}
              content-type: application/vnd.debian.binary-package
              content-length: {b['length']}
              content-md5: {b['md5']}
              last-modified: {b['mtime']}
              etag: {b['etag']}
              x-ms-request-id: {reqid}
              x-ms-version: 2025-05-05
              x-azure-ref: {ref}
              x-cache: CONFIG_NOCACHE
              accept-ranges: bytes
            """)
    else:
        hdr = textwrap.dedent(f"""\
              HTTP/1.1 200 OK
              Content-Length: {b['length']}
              Content-Type: application/vnd.debian.binary-package
              Content-MD5: {b['md5']}
              Last-Modified: {b['mtime']}
              ETag: {b['etag']}
              Server: Windows-Azure-Blob/1.0 Microsoft-HTTPAPI/2.0
              x-ms-request-id: {reqid}
              x-ms-version: 2025-05-05
              x-azure-ref: {ref}
              Date: {date}
            """)
    return (f"Spider mode enabled. Check if remote file exists.\n"
            f"--{date}--  https://packages.trafficmanager.net/public/sai/libsaibcm.deb\n"
            f"Resolving packages.trafficmanager.net... 1.2.3.4\n"
            f"HTTP request sent, awaiting response... \n{hdr}"
            f"Length: {b['length']} [application/vnd.debian.binary-package]\nRemote file exists.\n\n")


def fingerprint(tmp_path, text, name="hdr"):
    f = tmp_path / f"{name}.txt"
    f.write_text(text)
    out = subprocess.run([str(SCRIPT), "https://example.invalid/a.deb"], check=True, capture_output=True,
                         text=True, env=dict(os.environ, REMOTE_ARTIFACT_HEADERS=str(f)))
    fp = out.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", fp), out.stdout
    return fp


def test_per_request_headers_and_header_case_do_not_change_the_key(tmp_path):
    a = fingerprint(tmp_path, wget_output(False, "Thu, 05 Nov 2026 03:56:29 GMT", "20261105T035628Z-1658d5bd", "aaa-1"), "a")
    b = fingerprint(tmp_path, wget_output(False, "Fri, 06 Nov 2026 11:00:00 GMT", "20261106T110000Z-1646d6f4", "bbb-2"), "b")
    c = fingerprint(tmp_path, wget_output(True, "Fri, 06 Nov 2026 11:00:00 GMT", "20261106T110000Z-1646d6f4", "ccc-3"), "c")
    assert a == b == c


def test_a_replaced_blob_changes_the_key(tmp_path):
    base = fingerprint(tmp_path, wget_output(False, "d", "r", "i"), "base")
    for change in (dict(etag='"0x8DF1EEA41AD4F1B"'), dict(length="64315417"),
                   dict(md5="x7HsjvIaFioYQzBTAS5I1Q=="), dict(mtime="Thu, 01 Oct 2026 00:00:00 GMT")):
        assert fingerprint(tmp_path, wget_output(False, "d", "r", "i", **change), "chg") != base, change


def test_two_urls_are_both_folded_in(tmp_path):
    one = fingerprint(tmp_path, wget_output(False, "d", "r", "i"), "one")
    two = fingerprint(tmp_path, wget_output(False, "d", "r", "i") + wget_output(False, "d", "r", "i", etag='"dev"'), "two")
    assert one != two


def test_empty_or_failed_response_still_prints_a_fingerprint(tmp_path):
    # wget failures used to hash to the sha256 of the error text; keep a stable, non-empty flag.
    assert fingerprint(tmp_path, "wget: unable to resolve host address\n", "err")


def test_sai_dep_files_use_the_script_and_depend_on_it():
    for dep in ("sai-xgs.dep", "sai-dnx.dep"):
        text = (ROOT / "platform" / "broadcom" / dep).read_text()
        assert "scripts/remote_artifact_fingerprint.sh $(" in text, dep
        assert "DEP_FILES += $(SONIC_COMMON_BASE_FILES_LIST) scripts/remote_artifact_fingerprint.sh" in text, dep
        assert "--server-response" not in text, dep
    assert os.access(SCRIPT, os.X_OK)
