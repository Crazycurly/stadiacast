# certs/

Optional. The receiver ships a built-in certificate; put your own pair here only to serve it
instead ([docs/setup.md](../docs/setup.md#certificate) says how to get one):

| file            | what                                                       |
|-----------------|------------------------------------------------------------|
| `fullchain.pem` | leaf + intermediate, issued by Google Trust Services (GTS) |
| `privkey.pem`   | its private key (keep it `chmod 600`)                      |

Everything here except this README is ignored by git. **Never commit a private key.**
