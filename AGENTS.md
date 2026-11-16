# RoadSense browser policy

- Use **Vivaldi** for all RoadSense dashboard opening, visual verification,
  responsive testing, browser automation and debugging.
- macOS application: `/Applications/Vivaldi.app`; automation executable:
  `/Applications/Vivaldi.app/Contents/MacOS/Vivaldi`.
- Configure Chromium-compatible tools with that explicit executable. Never
  automatically install or launch Google Chrome for Testing, bundled Chromium,
  or another fallback. If a tool cannot use Vivaldi, report the limitation.
- Testing must use a fresh temporary `--user-data-dir`, never the user's personal
  Vivaldi profile. Do not read or modify personal bookmarks, passwords, cookies,
  extensions or credentials. Use `--password-store=basic`, `--use-mock-keychain`,
  `--no-first-run`, `--no-default-browser-check`, and `--disable-extensions`.
- Close only the browser processes owned by the test, then remove their temporary
  profiles, including on failure. Never kill all Vivaldi processes.
- Start development servers with `--no-browser` during testing to avoid the OS
  browser opener reusing a personal profile. Open the URL in isolated Vivaldi.
- `PYTHONPATH=src venv/bin/python tests/browser_smoke.py` runs the existing browser
  regression in Vivaldi. Add `--headed` for a visible window. An alternate
  Vivaldi installation can be supplied with `--browser /path/to/Vivaldi`.
- Preserve the RoadSense 2.0 implementation and existing tests/development workflow.
