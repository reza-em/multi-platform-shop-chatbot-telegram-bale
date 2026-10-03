# Security Policy

## Supported versions

Only the latest commit on the `main` branch of the shop chatbot receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private reporting instead: open the **Security** tab of this repository and choose **Report a vulnerability**. Include a description, steps to reproduce, and the potential impact. You can expect an initial response within about a week.

## Handling secrets

This project talks to chat-platform APIs using bot tokens. If you discover a leaked token, key or credential in the code, history or an issue, report it privately as above, and revoke/rotate the credential right away. Tokens must only ever be supplied through environment variables or untracked local files, never committed to the repository.
