# Native file observation across platforms

The first native Windows installed-wheel run of the platform gate executed all
932 tests, with two failures and seven errors. Eight arose in native note reads;
one was an incorrectly constructed line-ending fixture. They were not hidden by
skipping a platform or changing the expected error to accept any failure.

A focused native Windows trace showed identical device, file ID, byte size and
mtime in all four observations. However, `lstat().st_ctime_ns` reported one clock
and `fstat().st_ctime_ns` another. Each was stable across its own before/after
samples. Comparing those unlike clocks caused ordinary completed writes to be
mistaken for edits during the read. Python documents Windows ctime's transition
from creation time toward metadata-change time; it is not a portable content
revision. The diagnosis here is based on the observed values, not a claim that
all Windows/Python versions return the same clock pair.

The reader continues to validate regular-file types, bounded size, full byte
count, and device/file identity/size/mtime across all observations. It checks
ctime stability between the two path samples and separately between the two
handle samples. No clock is dropped outright, no tolerance or retry is added,
and no exception is translated into an empty native-memory list. Later snapshot
comparison still hashes the content, so a same-length edit with restored mtime
continues to invalidate the original planning snapshot.

The line-ending test now establishes LF bytes before converting to CRLF. Replacing
LF inside already-CRLF bytes had created CR-CR-LF on Windows, changing the body
rather than just its representation. Its original parse-reuse and generation
assertions remain; the production Markdown parser is unchanged.

These changes affect deterministic observation, not memory semantics or authority.
Native sources remain read-only. Existing sharing restrictions, prompts, budgets,
source identities and default pipeline settings remain unchanged. Test coverage
includes both clocks changing within their own domains and identity/size/mtime
mismatches. Actual OS matrix outcomes must still be reported separately.
