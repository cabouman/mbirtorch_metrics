# Push credential for the regression harness (per-node, one-time)

The nightly regression harness pushes results to `mbirtorch_metrics` **non-interactively**, so it needs
a stored credential — GitHub no longer accepts a password. Do this once on each machine that runs the
harness (e.g., each cluster login you use). `create_token.sh` (same directory) automates step 2.

## 1. Create a Personal Access Token (PAT)

This repository is owned by the user account `cabouman`, and that decides which kind of token works.

**A fine-grained token can only target repositories owned by whoever creates it, or by an
organization that has opted in.**  So a fine-grained token created by `gbuzzard` cannot reach
`cabouman/mbirtorch_metrics`, however its permissions are set.  There are two workable routes.

**Route A, a classic token, which anyone with push access can create.**

1. GitHub, then avatar, then **Settings**, then **Developer settings**, then **Personal access
   tokens**, then **Tokens (classic)**, then **Generate new token (classic)**.
2. Give it a note, for example `mbirtorch_metrics nightly`, and choose an expiration.
3. Tick the **`repo`** scope.  Nothing else is needed.
4. Generate, then **copy the token** (`ghp_...`).  You cannot view it again.

A classic token acts with your own permissions across every repository you can reach, so keep the
expiration short and store it only in the credential file below.

**Route B, a fine-grained token, which the repository's owner creates.**

1. `cabouman` follows the fine-grained path, with **Resource owner:** `cabouman`.
2. **Repository access:** *Only select repositories*, then **`mbirtorch_metrics`**.
3. **Permissions**, then **Repository permissions**, then **Contents: Read and write**.  That is the
   only permission needed.
4. `cabouman` generates the token and passes it to whoever runs the nightly.

Route B is the tighter of the two, because the token reaches one repository only.

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
