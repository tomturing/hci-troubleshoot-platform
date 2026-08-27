from shared.observability import otel


def test_test_mode_does_not_create_remote_otlp_exporter(monkeypatch) -> None:
    def exporter_must_not_be_created(**_kwargs):
        raise AssertionError("测试模式不应创建远端 OTLP exporter")

    monkeypatch.setenv("HCI_TESTING", "1")
    monkeypatch.setattr(otel, "OTLPSpanExporter", exporter_must_not_be_created)
    monkeypatch.setattr(otel, "_otel_provider", None)

    otel.init_telemetry("unit-test")

    assert otel.get_otel_provider() is not None
