# Test fixtures

Binary media fixtures are never committed to Git. Tests that need real
containers generate them at runtime with FFmpeg (for example, a tiny MP4
synthesized from `lavfi` sources) and skip when FFmpeg is unavailable.
Transcript and selection fixtures are small deterministic Python literals
defined next to the tests that use them, so golden behavior stays readable in
the diff.

External processes are always faked at the runner boundary in unit tests; only
integration tests invoke real binaries, and they gate on `shutil.which`.
