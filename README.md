# MinMin Edit Video Full runtime

Independent release repository for MinMin's optional video workspace.
Run **Actions > Build Edit Video Full** with a version such as `v0.1.0`.
Windows x64 and Linux x64 builds run an actual browser-module and MP4 smoke test.
Successful builds create a **draft** release containing runtime ZIPs, checksummed
manifest and Concat source. Concat/FFmpeg binaries are excluded from MinMin ZIPs;
customers download their pinned original archives directly from upstream in the same
one-click installation. CI verifies this exact assembly before creating the draft.
Review the third-party notices and installation results, then publish. MinMin's Store downloads the published manifest; draft releases are
not installable by customers. No executable binaries are tracked in this repo.

The catalogue preserves all 18 reference repos. Runtime npm versions are pinned
separately in package-lock.json, not misrepresented as builds of the reference commits.
Concat is a separate, unmodified AGPL executable. Node, FFmpeg, browser and npm
dependencies retain their respective licences. Original Motion Video Director and
the collected sound library are not included.
