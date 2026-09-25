# KickClipper GitHub workflow

`main` is the last reviewed baseline. Development happens on
`launch-hardening` or a smaller feature/fix branch. Open a pull request into
`main`; merge only after CI, CodeQL and a staging VOD check pass. CodeQL currently
saves a SARIF report as a workflow artifact because GitHub Code Security is not
enabled on this private repository. Enable that feature before switching back
to direct code scanning uploads.

Never commit `.env`, databases, API keys, passwords, payment data, VODs,
transcripts, generated clips, logs, model weights or Python environments.

## Normal update

```bash
git switch launch-hardening
git pull
git switch -c fix/short-description
# make and test the change
git add -A
git commit -m "Fix: short description"
git push -u origin fix/short-description
```

Open a pull request into `launch-hardening` for ongoing hardening, or into
`main` for a release candidate. Test real provider calls and a representative
VOD in staging because CI deliberately uses no paid API credentials.

## Rollback

Use GitHub's revert button on the merged pull request, or locally:

```bash
git switch main
git pull
git revert <bad-commit-sha>
git push
```

The application is not production-ready merely because CI passes. Production
still requires an external database, durable job queue/workers, object storage,
TLS, secure cookies, CSRF/rate limiting, monitored backups, staging deployment,
and payment/webhook verification.
