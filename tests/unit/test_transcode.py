from voicelive_sip_gateway.media import transcode


def test_roundtrip_mulaw_pcm16():
    pcm_samples = bytes([0, 0, 255, 127, 0, 128, 1, 0])
    mulaw = transcode.pcm16_to_mulaw(pcm_samples)
    decoded = transcode.mulaw_to_pcm16(mulaw)
    assert len(decoded) == len(pcm_samples)
