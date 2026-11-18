from app.convert_store import ConversionStore, render_convert_status


def test_conversions_get_ids_states_and_reuse(tmp_path) -> None:
    store = ConversionStore(tmp_path)
    first = store.add("docs/a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
    done = store.update(first.id, state="converted", source_sha256="f" * 64,
                        output="raw/converted/docs/a.pdf.md")

    assert first.id == "C1" and first.state == "converting"
    assert store.latest_converted("docs/a.pdf", "f" * 64) == done
    assert store.latest_converted("docs/a.pdf", "e" * 64) is None
    status = render_convert_status([done], {"C1": "c1.json"}).decode()
    assert '"request": "c1.json"' in status and '"state": "converted"' in status


def test_recovery_fails_conversions_cut_by_a_restart(tmp_path) -> None:
    store = ConversionStore(tmp_path)
    running = store.add("a.pdf", request="c1.json", auto_run=0, round_n=1, agent="a" * 32)
    done = store.add("b.pdf", request="c2.json", auto_run=0, round_n=1, agent="a" * 32)
    store.update(done.id, state="converted", output="raw/converted/b.pdf.md")

    store.recover()

    states = {r.id: (r.state, r.reason) for r in store.load()}
    assert states == {running.id: ("failed", "interrupted by restart"), done.id: ("converted", None)}
