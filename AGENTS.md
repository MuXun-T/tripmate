# TripMate project workspace rules

## Project purpose

- TripMate is a local internship portfolio project focused on Agent design. Do not add deployment, hosting, or production operations work unless explicitly requested.

- Keep all TripMate project files and project-managed tooling on drive E under `E:\code\agent\tripmate-agent`.
- Put virtual environments, Python runtimes, package/download caches, generated artifacts, local service data, logs, and backups inside this project directory (for example, `.venv`, `.tools`, and `.cache`). Configure tools to use these locations instead of user-profile or system-drive defaults where possible.
- Do not install or store TripMate dependencies or data in `C:\` or in sibling projects. Do not modify `peakdispatch-agent` or other projects while working on TripMate.
- Preserve existing user files and environments. Do not delete or relocate existing content without explicit authorization.
- Some Windows-managed components, such as optional OS features, cannot be installed under the project directory. Do not enable or install such components as part of TripMate setup without first explaining that limitation and getting the user's approval.
- Before adding a tool or dependency, check whether its installation path and caches can be directed into this project. Report any unavoidable files or state outside the project instead of claiming the setup is fully E-drive-local.
