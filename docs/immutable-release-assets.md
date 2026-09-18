# Immutable release assets (FS184)

This change only tightens the existing GitHub Release publishing/retry path.
It is not a release request, an automatic version bump, a runtime memory feature,
or permission to bypass the installed-artifact matrix. The ordinary trigger
remains an authorized `release: v<version>` commit on `main`; all other commits
skip publishing. PyPI keeps its existing verified-CI trigger and upload route.

## Existing assets are never replaced

The former `gh release upload --clobber` retry deleted and reuploaded same-named
attachments. Matching the Git tag to the source commit alone did not establish
that the bytes were unchanged: rebuilding an archive can produce different
compressed bytes from equivalent source. A retry must use the **original verified
artifacts**, not a rebuild presented as the same published package.

`tests_public/release_assets.py` checks the tag's resolved commit and release ID,
then downloads all existing expected assets by their asset IDs. Sizes and SHA-256
must match the local wheel, source archive and deterministic `SHA256SUMS` before
any missing attachment is uploaded. A mismatch fails without deleting, renaming,
overwriting or repairing the published asset. Unrelated release attachments stay
untouched. Duplicate expected names, unfinished uploads and malformed metadata
fail visibly. This helper is CI-only and is included with the public tests, not
imported by the Memleaf runtime.

Uploads are create-only and bounded to one attempt per missing name. When another
uploader wins, the helper rereads and verifies the resulting bytes; a failed
command is not itself proof of either failure or success on the server. A missing
asset after an attempt is unconfirmed and stops the run. Subsequent retries inspect
again, preserve matching existing assets, and fill only the still-missing ones.
Creation of a new release also finishes with the same verification step.

The tag, release identity, asset metadata and local files are checked repeatedly;
a final stable observation is required. This is not a transaction against arbitrary
concurrent administrators, a signature, or protection against changes after the
last check. A failed run may have uploaded some new assets, so its error reports
`remote_outcome: recheck_required`, never “nothing was changed”. No rollback or
asset deletion is performed. A `starter` asset requires explicit operator review,
not an automatic destructive fix.

## Validation and limits

Pure local tests cover all-present equality, missing attachments, equal-size
content conflicts, target changes, recreated releases, incomplete assets, races,
partial uploads, local mutation and transport failures. No test creates a real
Release, tag or PyPI upload. The helper uses the already installed GitHub CLI and
Python standard library; it does not introduce an online model call or runtime
dependency. Its real publishing path remains unexecuted until a separately
authorized release. Published immutable-release settings, if enabled, may reject
missing uploads: that failure does not authorize a workaround or replacement.

Official references: `https://cli.github.com/manual/gh_release_upload` and
`https://docs.github.com/en/rest/releases/assets`. These describe `--clobber` as
delete/reupload and the binary asset download contract; this project adopts a
stricter create-only policy instead.
