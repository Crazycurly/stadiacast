# certs/

Optional. The receiver ships a built-in credential (see
[docs/setup.md](../docs/setup.md#certificate)); put a pair here only to serve your own instead:

| file            | what                                                           |
|-----------------|----------------------------------------------------------------|
| `fullchain.pem` | leaf + intermediate, issued by Google Trust Services (GTS)     |
| `privkey.pem`   | its private key (keep it `chmod 600`)                          |

When both files exist they are used instead of the built-in credential; `--cert`/`--key` name
a pair kept elsewhere. Everything in this directory except this README is ignored by git.
**Never commit a private key**, not even to a private repository.

The controller validates the chain against its built-in GTS roots and checks neither the
hostname, nor the validity dates, nor revocation (confirmed on a real controller), so a GTS
certificate for any domain you own works for every Google name the receiver stands in for and
never needs renewing. [docs/setup.md](../docs/setup.md#certificate) explains how to get one
for free with Google's public ACME endpoint.

For the tests and the fake controller a self-signed pair is generated on the fly
(`--self-signed`); the real controller rejects it with `unknown_ca`.
