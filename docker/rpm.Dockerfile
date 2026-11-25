FROM rockylinux/rockylinux:10@sha256:d24beedc58f79b9e0e9289fedaa4fcbcbe692b1438883a86f177fd7c51053ca8
RUN dnf -y install rpm-build rpm-sign createrepo_c gnupg2 python3 systemd shadow-utils util-linux && dnf clean all && systemctl mask systemd-binfmt.service
USER 65534:65534
