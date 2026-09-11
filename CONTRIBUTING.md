# Contributing

Thank you for contributing to memleaf.

By submitting a contribution, you confirm that you have the right to submit it and agree that your contribution is licensed under the same MIT License as the project.

Do not submit code or other material that you do not have the right to license under these terms.

## Maintainability

Keep package entry points thin and split modules/tests by responsibility before unrelated behaviors accumulate in one file. Regression coverage belongs in the repository, but test modules should stay reviewable. Reproducible performance investigations should use temporary or external benchmark artifacts rather than committed large-run result fixtures in `main`.
