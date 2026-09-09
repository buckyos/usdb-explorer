# Immutable legacy migration fixture

Downloaded read-only from `buckyos/usdb` release ID `384757089`, tag `usdb-public-v0.1.0`.
Producer run: `34232344555`, source `ec656e6ff6bdd0ba00806250e5b9e62bbbf048fc`.

- Asset `550565355`: `install-usdb-public-v0.1.0.sh`, SHA-256 `af145ff3de47356c9643660af6f0359c39c65c6c5c785b6c8897a260d3c6d97e`.
- Asset `550565364`: `usdb-public-v0.1.0.tar.gz`, SHA-256 `3ed799cd5e0592f7e55f66fd23f65d81c19f97fd35107e8bada679d9c4975c49`.

Keep these files byte-for-byte unchanged. They are used only for isolated installation/upgrade tests.
The test supplies local fake downloads, explicit temporary installation paths and fake Docker.
