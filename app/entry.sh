#!/bin/sh
# Start-up. With DHCP=1 (and the container started as root with NET_ADMIN) it first gets its
# address from the network's DHCP server, sending DHCP_HOSTNAME so the router can register the
# name. Then it drops to the unprivileged app user. Without DHCP=1 it just runs the app.
set -e
if [ "$(id -u)" = 0 ]; then
    if [ "${DHCP:-0}" = 1 ]; then
        IF="${DHCP_IF:-eth0}"
        # Docker gave the interface a placeholder address; drop it so only the DHCP lease is used
        ip -4 addr flush dev "$IF"
        ip -4 route flush dev "$IF" 2>/dev/null || true
        # udhcpc stays in the background and renews the lease; -b = keep trying if the first try fails.
        # IF_PEER_DNS=no: keep Docker's own DNS resolver.
        IF_PEER_DNS=no udhcpc -i "$IF" -b -t 5 -T 3 -A 10 -x hostname:"${DHCP_HOSTNAME:-contact-sync}"
    fi
    chown app:app /data 2>/dev/null || true
    exec su-exec app python /app/main.py
fi
exec python /app/main.py
