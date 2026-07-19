# Publishing via a Launchpad PPA

A PPA distributes the app through `apt` on Ubuntu. Launchpad **builds from
source**, so the `debian/` directory in this repo is what it uses. This has been
test-built locally (`dpkg-buildpackage -b`) and produces a working `.deb`.

## One-time setup

### 1. SSH key (code hosting / git-over-ssh)
Already generated at `~/.ssh/id_ed25519`. Import the **public** key at
<https://launchpad.net/~/+editsshkeys> (paste `~/.ssh/id_ed25519.pub`).

### 2. GPG key (required — PPA uploads must be signed)
A signing key has already been generated and published to the Ubuntu keyserver:

- **Name/email:** `Daniel Houtmann <danielhoutmann@hotmail.com>`
- **Fingerprint:** `D42A012CF26518F44F1E4F7BB1174D503445F8FE`
- Secret key lives in `~/.gnupg` on this machine (no passphrase — add one with
  `gpg --change-passphrase D42A012CF26518F44F1E4F7BB1174D503445F8FE` if you like).

Register it: paste the fingerprint at <https://launchpad.net/~/+editpgpkeys>.
Launchpad emails an **encrypted** confirmation — decrypt + click to confirm:

```bash
# decrypt the confirmation email body you receive:
gpg --decrypt < the-email-body.txt
```

### 3. Create the PPA
On <https://launchpad.net/~ZoutMax> → *Create a new PPA* (e.g. name `fifine`).
It becomes `ppa:zoutmax/fifine`.

### 4. Tell dput about it (usually automatic)
`dput ppa:zoutmax/fifine …` works out of the box on Ubuntu.

## Build + upload the source package

The version and series come from the top entry of `debian/changelog` (check
with `head -1 debian/changelog`; `./release.sh <version>` maintains it). Sign
with the key explicitly (`-k`), which avoids the maintainer-email/key mismatch:

```bash
cd FifineControlDeck
KEY=D42A012CF26518F44F1E4F7BB1174D503445F8FE
VERSION="$(sed -n '1s/.*(\([^)]*\)).*/\1/p' debian/changelog)"

# build a SIGNED source package (.dsc + _source.changes):
debuild -S -k$KEY

# upload to your PPA:
dput ppa:zoutmax/fifine "../fifine-control-deck_${VERSION}_source.changes"
```

This was verified locally end-to-end (build + sign succeed); only the `dput`
step needs your Launchpad account + a confirmed GPG key.

### Re-uploads / other series
Launchpad rejects a re-used version. For a new upload bump the version *upward*
(no leading `~`, which sorts lower), and target each series you want:

```bash
export DEBEMAIL="danielhoutmann@hotmail.com" DEBFULLNAME="Daniel Houtmann"
dch -v 0.5.2ppa2 --distribution jammy "PPA build for jammy"
debuild -S -k$KEY
dput ppa:zoutmax/fifine ../fifine-control-deck_0.5.2ppa2_source.changes
```

Launchpad then builds the binaries for `amd64` and `arm64` (per `debian/control`)
and publishes them. Users install with:

```bash
sudo add-apt-repository ppa:zoutmax/fifine
sudo apt update
sudo apt install fifine-control-deck
```

## Notes
- **Per-series builds:** repeat the `dch --distribution <series>` + `debuild -S`
  + `dput` for each Ubuntu series you want (noble, jammy, oracular, …). Bump the
  version suffix (`ppa1`, `ppa2` — **no leading `~`**: a tilde sorts *below*
  the base version, so apt would treat `0.5.7~ppa1` as older than `0.5.7` and
  Launchpad would reject it as a downgrade) on re-uploads.
- **Maintainer email:** `debian/control` uses the GitHub no-reply address; the
  *signed upload* uses your GPG identity (`DEBEMAIL`). Launchpad cares about the
  signing key, not the Maintainer field.
- The same `debian/` tree also builds a local `.deb` with `dpkg-buildpackage -b`
  (unsigned) if you just want to test.
