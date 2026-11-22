

def test_slot_follows_whichever_address_answers():
    # A model swap can move a local server between :8000 and :8888; the slot
    # must follow whichever address answers.
    import io, json as _json
    from pulsar.daemon import slots as _slots
    from pulsar import models as _models

    def opener(url, timeout=None):
        if ":8888/" in url:
            return io.BytesIO(_json.dumps({"data": [{"id": "deepseek-v4.1-flash"}]}).encode())
        raise OSError("refused")

    ring = _models.model_by_id("gpu-ring")
    assert len(ring.base_urls) >= 2
    status = _slots.probe_slot(ring, opener=opener)
    assert status["live"] and status["served_id"] == "deepseek-v4.1-flash"
    assert status["base_url"].endswith(":8888/v1")
