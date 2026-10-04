# Network Configuration (RoCEv2, MTU 9000, WLAN hardening)
- Jumbo Frames MTU 9000 for ConnectX-7 RoCEv2
- DMA-BUF zero-copy
- WLAN emergency routing: primary metric 100, wlan metric 600, nftables restrict wlan to Master Anchor
