# Push credential for the regression harness (per-node, one-time)

The nightly regression harness pushes results to `mbirtorch_metrics` **non-interactively**, so it needs
a stored credential — GitHub no longer accepts a password. Do this once on each machine that runs the
harness (e.g., each cluster login you use). `create_token.sh` (same directory) automates step 2.

## 1. Create a fine-grained Personal Access Token (PAT)

1. GitHub → avatar → **Settings** → **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**.
2. **Resource owner:** `gbuzzard`.
3. **Repository access:** *Only select repositories* → **`cabouman/mbirtorch_metrics`**.
4. **Permissions** → **Repository permissions** → **Contents: Read and write** (the only one needed —
   keep the scope minimal).
5. Choose an expiration, generate, and **copy the token** (`github_pat_…`) — you can't view it again.

## 2. Store it for the harness

Run `bash create_token.sh` and paste the token when prompted (it offers to show these instructions if
you press Enter without a token). Or do it by hand:

```
mkdir -p ~/.config/mbirtorch && chmod 700 ~/.config/mbirtorch
umask 077
printf 'https://%s:%s@github.com\n' '<your-github-username>' '<PAT>' > ~/.config/mbirtorch/metrics_credentials
chmod 600 ~/.config/mbirtorch/metrics_credentials
```

This writes a git **credential-store** file — one line `https://<user>:<token>@github.com` (the
`https://…@github.com` form, *not* the bare token). The wrapper points git at it via
`git config credential.helper "store --file=$TOKEN_FILE"`; `TOKEN_FILE` defaults to
`~/.config/mbirtorch/metrics_credentials` in `tooling/regression/regression.env`.

## 3. Verify (from any `mbirtorch_metrics` clone)

```
GIT_TERMINAL_PROMPT=0 git -c credential.helper="store --file=$HOME/.config/mbirtorch/metrics_credentials" push --dry-run
```

**The token is working if you see EITHER** `Everything up-to-date` **OR** `! [rejected] ... (fetch first)`
— both mean git reached the remote and authenticated; "rejected" just means that clone is behind
origin (run `git pull --rebase` first for a clean check; the harness does this automatically before
every push). **Only** `fatal: Authentication failed` or a username/password prompt means the token,
username, or file format is wrong.

## Notes
- The token is plaintext in a `chmod 600` file — standard for git's credential store; the minimal
  scope (one repo, Contents only) keeps the blast radius tiny.
- **Never commit the credential file.** It lives in `~/.config`, outside any repo.
- On expiry/rotation, re-run `create_token.sh`. A failed push is non-fatal: the run's results stay in
  the persistent work clone (`~/.mbirtorch/regression/metrics`) and push on the next successful run.
