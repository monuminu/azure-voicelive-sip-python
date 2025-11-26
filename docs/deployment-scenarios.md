# Voice Live SIP Gateway Deployment Scenarios

This document captures two common topologies for the gateway: a lightweight local testing setup without a SIP server, and a production-ready deployment that adds SBC and PBX infrastructure.

## Local Testing (Direct SIP)

```
┌─────────────────┐
│  SIP Softphone  │ (e.g., X-Lite, Zoiper, MicroSIP)
│  192.168.1.4    │
└────────┬────────┘
         │ Direct SIP connection (no server)
         │ sip:test@127.0.0.1:5060
         │
┌────────▼────────────────────┐
│  Voice Live VoIP Gateway    │ (Python app)
│  127.0.0.1:5060             │
│  - SIP stack                │
│  - RTP media handler        │
│  - Audio transcoding        │
└────────┬────────────────────┘
         │ HTTPS
         │
┌────────▼────────────────────┐
│  Azure Voice Live API       │
│  <resource>.cognitives...   │
└─────────────────────────────┘
```

**Configuration notes**

```powershell
$env:REGISTER_WITH_SIP_SERVER = "false"  # No SIP server
$env:SIP_LOCAL_ADDRESS = "127.0.0.1"     # Direct connection
```

## Production (SIP Server + SBC)

```
┌──────────────┐
│ PSTN / Users │ (Phone network)
└──────┬───────┘
       │
┌──────▼───────────────────────┐
│  SBC (Session Border Ctrl)   │ (e.g., Audiocodes, Oracle, Ribbon)
│  - NAT traversal             │
│  - Security / firewall       │
│  - Protocol conversion       │
│  - Media anchoring           │
│  - Topology hiding           │
└──────┬───────────────────────┘
       │ SIP trunk
       │
┌──────▼───────────────────────┐
│  SIP Server / PBX            │ (e.g., Asterisk, FreeSWITCH, Teams)
│  - Call routing              │
│  - User directory            │
│  - Call features             │
│  - CDR logging               │
└──────┬───────────────────────┘
       │ SIP INVITE
       │ sip:bot@gateway.example.com
       │
┌──────▼───────────────────────┐
│  Voice Live VoIP Gateway     │ (Python app)
│  gateway.example.com:5060    │
│  - Registers with SIP server │
│  - Receives INVITEs          │
└──────┬───────────────────────┘
       │ HTTPS
       │
┌──────▼───────────────────────┐
│  Azure Voice Live API        │
└──────────────────────────────┘
```

**Production configuration**

```powershell
# SIP server configuration
$env:SIP_SERVER = "sip.example.com"
$env:SIP_PORT = "5060"
$env:SIP_USER = "voicelive-bot@sip.example.com"
$env:AUTH_USER = "voicelive-bot"
$env:AUTH_REALM = "sip.example.com"
$env:AUTH_PASSWORD = "your-password"
$env:REGISTER_WITH_SIP_SERVER = "true"
$env:DISPLAY_NAME = "Voice Live Bot"

# Network configuration (for SBC/NAT)
$env:MEDIA_ADDRESS = "your-public-ip"
$env:SIP_VIA_ADDR = "your-public-ip"
$env:SIP_LOCAL_ADDRESS = "0.0.0.0"
```

## Why Add SBC + SIP Server?

### SBC Benefits
- **NAT traversal**: Handles RTP through firewalls
- **Security**: Shields internal network from malicious SIP traffic
- **Protocol translation**: Bridges different SIP dialects
- **Media anchoring**: Optionally transcodes codecs
- **Topology hiding**: Masks private IPs

### SIP Server / PBX Benefits
- **Call routing** to the gateway based on IVR logic
- **User/auth management** for SIP credentials
- **Advanced call features** (hold, transfer, conferencing)
- **Analytics & logging** for regulatory/compliance needs

## Example Production Call Flow

```
1. Customer dials +1-800-123-4567
2. PSTN hands call to SBC (PSTN → SIP)
3. SBC forwards to SIP server based on trunk rules
4. SIP server routing sends INVITE to sip:voicebot@gateway.example.com
5. Voice Live gateway answers, establishes RTP
6. Gateway streams audio bidirectionally with Azure Voice Live
7. AI responses are synthesized and sent back through SIP path
```
