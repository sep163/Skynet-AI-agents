import json
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, config, emotion, llm, ops as ops_mod, voice
from .network import FAULT_KINDS, PARENT, all_ports
from .tools import _clean

app = FastAPI(title="SkyNet Sentinel + AI Support")


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    client_id: str
    messages: list[Msg]
    voice: dict | None = None


class StepIn(BaseModel):
    minutes: int = 5


class InjectIn(BaseModel):
    kind: str
    target: str


class DecideIn(BaseModel):
    approve: bool
    comment: str = ""


class TextIn(BaseModel):
    text: str


@app.get("/api/health")
def health():
    return {"ok": True, "provider": config.PROVIDER, "model": config.CLAUDE_MODEL if config.PROVIDER == "claude" else config.OLLAMA_MODEL,
            "voice": voice.available()}


@app.get("/api/state")
def state():
    o = ops_mod.get()
    return _clean({
        "network": o.net.summary(), "autonomous": o.autonomous,
        "incidents": sorted(o.sentinel.incidents, key=lambda i: (i["status"] != "open", -i["detected_at"].timestamp())),
        "actions": o.actions[::-1][:60], "jobs": sorted([j for j in o.jobs if j["status"] != "cancelled"], key=lambda j: j["start"]),
        "technicians": [{k: t[k] for k in ("id", "name", "skills", "zones", "shift")} for t in o.techs],
        "notifications": o.notifications[::-1][:20], "tickets": o.tickets[::-1][:20],
        "fault_kinds": FAULT_KINDS, "targets": {"port": all_ports(), "switch": list(PARENT), "agg": sorted(set(PARENT.values()))},
        "faults": o.net.faults,
    })


@app.post("/api/sim/step")
def step(x: StepIn):
    ops_mod.get().tick(max(1, min(x.minutes, 600)))
    return state()


@app.post("/api/sim/inject")
def inject(x: InjectIn):
    if x.kind not in FAULT_KINDS:
        raise HTTPException(400, "неизвестный тип неисправности")
    return _clean(ops_mod.get().net.inject(x.kind, x.target))


@app.post("/api/sim/reset")
def reset():
    ops_mod.reset()
    return state()


@app.post("/api/sim/autonomy")
def autonomy(x: DecideIn):
    ops_mod.get().autonomous = x.approve
    return {"autonomous": x.approve}


@app.post("/api/actions/{aid}/decide")
def decide(aid: str, x: DecideIn):
    try:
        return _clean(ops_mod.get().decide(aid, x.approve, x.comment))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs/{jid}/complete")
def complete(jid: str):
    return _clean(ops_mod.get().complete_job(jid))


@app.post("/api/incidents/{iid}/brief")
def brief(iid: str):
    o = ops_mod.get()
    inc = next((i for i in o.sentinel.incidents if i["id"] == iid), None)
    if not inc:
        raise HTTPException(404)
    cl, subs = o.clients_on(inc["target"])
    ctx = {"incident": inc, "telemetry_last_30m": list(o.net.hist[inc["target"]])[-30:],
           "affected": {"subscribers": subs, "b2b": [c["name"] for c in cl if c["type"] == "b2b"]},
           "actions": [a for a in o.actions if a["incident_id"] == iid]}
    text = llm.complete("Ты — аналитик NOC интернет-провайдера. По данным инцидента дай дежурному разбор в 5 пунктах: что происходит; вероятная причина; "
                        "кого затрагивает (выдели B2B); рекомендуемое решение и кто его делает; что будет, если не действовать и к какому сроку. Коротко, по-русски.",
                        json.dumps(_clean(ctx), ensure_ascii=False, default=str))
    return {"incident_id": iid, "brief": text}


@app.get("/api/clients")
def clients():
    o = ops_mod.get()
    return [o.profile(c) | {"status": o.clients[c]["status"]} for c in o.clients]


@app.post("/api/chat")
def chat(x: ChatIn):
    try:
        return agent.answer(x.client_id, [m.model_dump() for m in x.messages], x.voice)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/emotion")
def emo(x: TextIn):
    return emotion.analyze(x.text)


@app.post("/api/transcribe")
def transcribe(audio: UploadFile = File(...)):
    if not voice.available():
        raise HTTPException(501, "локальная транскрибация не установлена: pip install -r requirements-voice.txt")
    data = audio.file.read()
    suffix = "." + (audio.filename or "a.webm").rsplit(".", 1)[-1]
    return voice.transcribe(data, suffix)


app.mount("/", StaticFiles(directory=config.ROOT / "static", html=True), name="static")
