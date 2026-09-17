# Validation record

Validated against repository base `3f4a21f03ae3d565aca37cc300e3d38b0c7b582a`.

- **63 feature-focused unit/API/authoring tests passed**, with 100% statement coverage of `app/backend/notifications/`.
- **Frontend production build passed** (`npm run build`, including TypeScript compilation).
- **Python type checks, Ruff, Black, and whitespace checks passed** for changed Python code.
- All seven environment settings were checked for mappings in Bicep, deployment parameters, and both CI pipelines.
- The example JSON validates and the checked-in editor schema matches the Python models.

The test environment has no usable `/proc` process/CPU information. A temporary test
launcher mocked `psutil.Process` and `psutil.cpu_times` only while importing Azure
telemetry; application code was not changed for this environment. Tests still used the
repository's normal Azure service mocks and Quart test clients.

The Markdown component was also rendered with React's server renderer through Vite.
Checks passed for inline HTTPS/relative links, emphasis, a 40,000-character message,
and seven unsafe HTML/link/image inputs. Browser auth initialization was stubbed for
these rendering checks; the actual notification component and Markdown renderer ran.

Browser tests were added for responsive placement, severity, literal text rendering,
Markdown links, accessibility, dismissal/content changes, expiry, and invalid API responses.
**They have not been executed successfully:** browser installation failed in this
environment. Run `pytest tests/e2e.py -k notification` with Playwright Chromium installed
before merging. The Bicep compiler and live Azure storage/managed-identity deployment
were also unavailable, so those remain deployment validation steps.

No production deployment was performed. The feature defaults to disabled.
