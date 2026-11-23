import datetime
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
if sys.platform == 'win32':
    sys.modules['fcntl'] = types.SimpleNamespace(flock=lambda *a: None, LOCK_EX=2, LOCK_NB=4)

spec = importlib.util.spec_from_file_location("t", sys.argv[1])
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

CFG = t.json.loads(t.json.dumps(t.DEFAULTS))
TUE_2330 = datetime.datetime(2026, 10, 6, 23, 30).timestamp()      # terca-feira
NOW = TUE_2330


def dt(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi)


# ---------------------------------------------------------------- janela
W = CFG["window"]
assert t.window_remaining(dt(2026, 10, 6, 23, 30), W) == 6.5 * 3600            # ter 23:30
assert t.window_remaining(dt(2026, 10, 7, 3, 0), W) == 3 * 3600                # qua 03:00 (janela de ter)
assert t.window_remaining(dt(2026, 10, 9, 2, 0), W) == 4 * 3600                # sex 02:00 (janela de qui)
assert t.window_remaining(dt(2026, 10, 10, 2, 0), W) is None                   # sab 02:00 (janela de sex: nao)
assert t.window_remaining(dt(2026, 10, 9, 23, 30), W) is None                  # sex 23:30
assert t.window_remaining(dt(2026, 10, 6, 12, 0), W) is None                   # meio-dia
assert t.window_remaining(dt(2026, 10, 5, 5, 59), W) == 60                    # seg 05:59 (janela de dom: agora sim)
assert t.window_remaining(dt(2026, 10, 4, 23, 30), W) == 6.5 * 3600            # dom 23:30
assert t.window_remaining(dt(2026, 10, 10, 23, 30), W) is None                 # sab 23:30 (cruza com o job semanal do pve3: nao)
assert t.window_remaining(dt(2026, 10, 6, 6, 0), W) is None

# ---------------------------------------------------------------- inventario com arquivos de verdade
tmp = tempfile.mkdtemp()


def make_source(name, files, status=None, age_days=2):
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, "dump"))
    for fname, size in files:
        p = os.path.join(root, "dump", fname)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        os.utime(p, (NOW - age_days * 86400, NOW - age_days * 86400))
    if status is not None:
        with open(os.path.join(root, CFG["status_file"]), "w") as f:
            json.dump(status, f)
    return {"dump": os.path.join(root, "dump"), "root": root}


def G(vmid, name, tags=None, state="ok"):
    """Padrao: VM marcada com a tag 'testar' (o testador so testa VMs marcadas)."""
    return {"vmid": str(vmid), "name": name, "tags": ["testar"] if tags is None else tags, "state": state}


st1 = {"generated": NOW - 60, "guests": [G(1000, "srv16"), G(1001, "srv15", ["sem-backup", "testar"], "isento"), G(1002, "srv11", []),
                                         G(1004, "srv9", ["sem-backup"], "isento"), G(1007, "RK02", state="sem_job")]}
src1 = make_source("pve1", [
    ("vzdump-qemu-1000-2026_10_03-01_00_02.vma.zst", 5000), ("vzdump-qemu-1000-2026_09_26-01_00_02.vma.zst", 4000),
    ("vzdump-qemu-1001-2026_10_03-01_01_00.vma.zst", 3000), ("vzdump-qemu-1002-2026_10_03-01_02_00.vma.zst", 9000),
    ("vzdump-qemu-1004-2026_10_03-01_22_00.vma.zst", 20000), ("vzdump-qemu-1007-2026_10_03-02_00_00.vma.zst", 100),
    ("vzdump-qemu-1000-2026_10_03-01_00_02.vma.zst.notes", 5), ("vzdump-qemu-1000-2026_10_03-01_00_02.log", 5), ("lixo.txt", 1)], st1)
st3 = {"generated": NOW - 60, "guests": [G(101, "site-PD1"), G(3006, "srv3"), G(3015, "srv34")]}
src3 = make_source("pve3", [
    ("vzdump-qemu-101-2026_10_04-01_00_01.vma.zst", 150000), ("vzdump-lxc-3006-2026_10_04-01_33_00.tar.zst", 1100),
    ("vzdump-qemu-3015-2026_10_01-02_00_00.vma", 241000), ("vzdump-qemu-3999-2026_10_04-01_50_00.vma.zst", 700)], st3)   # 3999: VM removida
st2 = {"generated": NOW - 60, "guests": [G(150, "srv22"), G(102, "dc-like")]}
src2 = make_source("pve2", [("vzdump-qemu-150-2026_10_06-21_00_00.vma.zst", 11000), ("vzdump-qemu-102-2026_09_20-20_00_00.vma.lzo", 143000)], st2)
srcN = make_source("nostatus", [("vzdump-qemu-777-2026_10_04-01_00_00.vma.zst", 500)], status=None)                    # sem estado no NAS

d = t.scan_dump_dir(src1["dump"])
assert sorted(d) == ["1000", "1001", "1002", "1004", "1007"] and d["1000"]["fname"].startswith("vzdump-qemu-1000-2026_10_03")
assert d["1000"]["size"] == 5000 and d["1000"]["kind"] == "qemu"
inv = t.build_inventory({"pve1": src1, "pve3": src3, "pve2": src2}, CFG, NOW)
by = dict((e["key"], e) for e in inv)
assert by["pve1/1000"]["skip"] == "" and by["pve1/1000"]["name"] == "srv16"
assert by["pve1/1001"]["skip"] == "isento" and by["pve1/1004"]["skip"] == "isento"      # tag sem-backup / estado isento
assert by["pve1/1002"]["skip"] == "nao-marcada"                                          # sem a tag 'testar'
assert by["pve1/1001"]["skip"] == "isento"                                               # sem-backup vence a tag 'testar' 
assert by["pve1/1007"]["skip"] == "sem-job"
assert by["pve3/3999"]["skip"] == "vm-removida"
assert by["pve2/150"]["skip"] == "" and by["pve2/150"]["status_known"] is True
nos = dict((e["key"], e) for e in t.build_inventory({"NOSTATUS": srcN}, CFG, NOW))["NOSTATUS/777"]
assert nos["skip"] == "sem-estado" and nos["status_known"] is False                     # nao le as tags: nao testa nada
cfg_all = dict(CFG, require_tag="")
assert t.build_inventory({"NOSTATUS": srcN}, cfg_all, NOW)[0]["skip"] == ""             # modo 'testa tudo' ignora tags
assert by["pve3/3006"]["kind"] == "lxc" and by["pve3/3015"]["fname"].endswith(".vma")
# estado velho (> 24 h) equivale a ausente
old = dict(st1, generated=NOW - 90000)
assert t.load_status(src1["root"], CFG, NOW) is not None
json.dump(old, open(os.path.join(src1["root"], CFG["status_file"]), "w"))
assert t.load_status(src1["root"], CFG, NOW) is None
json.dump(st1, open(os.path.join(src1["root"], CFG["status_file"]), "w"))
# padrao: so as VMs marcadas com a tag 'testar'
assert CFG["require_tag"] == "testar"
assert t.decide_skip("1000", {"1000": G(1000, "a", [])}, CFG) == "nao-marcada"
assert t.decide_skip("1000", {"1000": G(1000, "a", ["outra"])}, CFG) == "nao-marcada"
assert t.decide_skip("1000", {"1000": G(1000, "a", ["testar"])}, CFG) == ""
assert t.decide_skip("1000", {"1000": G(1000, "a", ["testar", "sem-backup"])}, CFG) == "isento"
assert t.decide_skip("1000", None, CFG) == "sem-estado"
assert t.decide_skip("1000", {"1000": G(1000, "a", [])}, cfg_all) == "" and t.decide_skip("1000", None, cfg_all) == ""

# ---------------------------------------------------------------- planejamento
state = t.new_state()
plan = t.plan_tests(inv, state, CFG, NOW)
keys = [p["entry"]["key"] for p in plan]
assert "pve1/1001" not in keys and "pve1/1002" not in keys and "pve1/1004" not in keys and "pve1/1007" not in keys
assert "pve3/3999" not in keys                                                            # VM removida nunca entra
assert keys[0] == "pve3/3006" and set(keys) == {"pve1/1000", "pve3/101", "pve3/3006", "pve3/3015", "pve2/150", "pve2/102"}
sizes = [p["entry"]["size"] for p in plan]
assert sizes == sorted(sizes), "nunca testadas: menores primeiro"
assert all(p["level"] == "integrity" for p in plan)                                      # restore desligado
# backup antigo demais (VM sem job ha tempo) nao e testado
assert "pve2/102" in keys
old_inv = [dict(e, ts=int(NOW - 41 * 86400)) if e["key"] == "pve2/102" else e for e in inv]
assert "pve2/102" not in [p["entry"]["key"] for p in t.plan_tests(old_inv, state, CFG, NOW)]
# integridade: backup novo e verificado na mesma noite (intervalo minimo de 1 dia, com meio dia de tolerancia); limite duro de 30 dias; falha reentra
e = by["pve1/1000"]
state["vms"]["pve1/1000"] = {"integrity": {"ts": int(NOW - 3 * 86400), "ok": True, "file": e["fname"]}, "last": {"ok": True}}
assert "pve1/1000" not in [p["entry"]["key"] for p in t.plan_tests(inv, state, CFG, NOW)]       # mesmo backup: nada a fazer
state["vms"]["pve1/1000"]["integrity"]["file"] = "vzdump-qemu-1000-antigo.vma.zst"
assert "pve1/1000" in [p["entry"]["key"] for p in t.plan_tests(inv, state, CFG, NOW)]           # backup novo: verifica
state["vms"]["pve1/1000"]["integrity"]["ts"] = int(NOW - 6 * 3600)
assert "pve1/1000" not in [p["entry"]["key"] for p in t.plan_tests(inv, state, CFG, NOW)]       # verificada ha 6 h: espera
state["vms"]["pve1/1000"]["integrity"]["ts"] = int(NOW - 23.5 * 3600)
assert "pve1/1000" in [p["entry"]["key"] for p in t.plan_tests(inv, state, CFG, NOW)]           # rodada de ontem foi 30 min depois: nao pula a noite
state["vms"]["pve1/1000"]["integrity"].update({"file": e["fname"], "ts": int(NOW - 31 * 86400)})
assert "pve1/1000" in [p["entry"]["key"] for p in t.plan_tests(inv, state, CFG, NOW)]           # 31 dias
state["vms"]["pve1/1000"]["integrity"].update({"ok": False, "ts": int(NOW - 3600)})
p0 = t.plan_tests(inv, state, CFG, NOW)
assert "pve1/1000" in [p["entry"]["key"] for p in p0]
# restore: a cada backup novo; VM grande so 1x por semana; falha repete na noite seguinte
cfg_r = t.json.loads(t.json.dumps(CFG))
cfg_r["restore"]["enabled"] = True
pr = dict((p["entry"]["key"], p["level"]) for p in t.plan_tests(inv, t.new_state(), cfg_r, NOW))
assert pr["pve1/1000"] == "restore" and pr["pve3/3006"] == "integrity"                     # CT: so integridade
fn = by["pve1/1000"]["fname"]


def plan_keys(st, cfg=cfg_r):
    return dict((p["entry"]["key"], p["level"]) for p in t.plan_tests(inv, st, cfg, NOW))


def vm_state(restore, **extra):
    r = {"restore": restore, "integrity": {"ts": int(NOW - 10 * 86400), "ok": True, "file": fn}}
    r.update(extra)
    st = t.new_state()
    st["vms"]["pve1/1000"] = r
    return st


assert "pve1/1000" not in plan_keys(vm_state({"ts": int(NOW - 10 * 86400), "ok": True, "file": fn}))            # restore do backup atual: nada a fazer
assert plan_keys(vm_state({"ts": int(NOW - 86400), "ok": True, "file": "antigo"}))["pve1/1000"] == "restore"     # backup novo: restaura
assert "pve1/1000" not in plan_keys(vm_state({"ts": int(NOW - 2 * 3600), "ok": True, "file": "antigo"}))        # restaurada ha 2 h: espera
assert plan_keys(vm_state({"ts": int(NOW - 23.5 * 3600), "ok": True, "file": "antigo"}))["pve1/1000"] == "restore"  # nao pula a noite
assert "pve1/1000" not in plan_keys(vm_state({"ts": int(NOW - 2 * 3600), "ok": False}))                         # falhou ha 2 h: espera
assert plan_keys(vm_state({"ts": int(NOW - 23.5 * 3600), "ok": False}))["pve1/1000"] == "restore"               # falhou ontem: tenta de novo hoje
big = vm_state({"ts": int(NOW - 3 * 86400), "ok": True, "file": "antigo"}, prov_gb=300.0)
assert "pve1/1000" not in plan_keys(big)                                                                       # VM grande: so 1x por semana
big["vms"]["pve1/1000"]["restore"]["ts"] = int(NOW - 6.8 * 86400)
assert plan_keys(big)["pve1/1000"] == "restore"
huge = vm_state({"ts": int(NOW - 9 * 86400), "ok": True, "file": "antigo"}, prov_gb=1500.0, restore_skip="grande")
assert "pve1/1000" not in plan_keys(huge)                                                                      # acima do limite maximo: dispensada
cfg_big = t.json.loads(t.json.dumps(cfg_r))
cfg_big["restore_max_gb"] = 2000
assert plan_keys(huge, cfg_big)["pve1/1000"] == "restore"                                                      # limite aumentado: volta
assert "pve1/1000" not in plan_keys(vm_state({"ts": int(NOW - 9 * 86400), "ok": True, "file": "antigo"}, restore_skip="tamanho desconhecido"))
# o tamanho provisionado e guardado mesmo quando o restore e dispensado
sx = t.new_state()
t.record_result(sx, by["pve1/1000"], "restore", {"ok": False, "skip": "grande", "prov_gb": 1500.0, "msg": "x"}, NOW)
assert sx["vms"]["pve1/1000"]["prov_gb"] == 1500.0 and sx["vms"]["pve1/1000"]["restore_skip"] == "grande"

# ---------------------------------------------------------------- comandos de integridade
P = "/m/dump/vzdump-qemu-1-2026_10_03-01_00_00"
assert t.integrity_cmds(P + ".vma.zst") == [["zstd", "-q", "-d", "-c", P + ".vma.zst"], ["vma", "verify", "-"]]
assert t.integrity_cmds(P + ".vma.lzo") == [["lzop", "-d", "-c", P + ".vma.lzo"], ["vma", "verify", "-"]]
assert t.integrity_cmds(P + ".vma.gz")[0][0] == "gzip" and t.integrity_cmds(P + ".vma") == [["vma", "verify", P + ".vma"]]
assert t.integrity_cmds(P.replace("qemu", "lxc") + ".tar.zst")[1] == ["tar", "-tf", "-"]
assert t.integrity_cmds(P + ".tar") == [["tar", "-tf", P + ".tar"]]
try:
    t.integrity_cmds(P + ".iso")
    raise SystemExit("deveria recusar formato desconhecido")
except ValueError:
    pass


class FakeRunner:
    """Simula o Proxmox. 'vms' guarda as VMs de teste criadas; todos os comandos ficam em .calls."""

    def __init__(self, pipe=None, **opt):
        self.calls, self.pipe, self.opt = [], pipe or (lambda cmds, cap: ([0] * len(cmds), "", "", 1.0)), opt
        self.vms, self.nets_deleted = {}, False
        self.protection0, self.deleted = False, set()

    def pipeline(self, cmds, timeout, capture=False):
        self.calls.append(("PIPE", [list(c) for c in cmds]))
        return self.pipe(cmds, capture)

    def run(self, cmd, timeout=120):
        self.calls.append(list(cmd))
        o = self.opt
        c = cmd
        if c[:2] == ["qm", "list"]:
            rows = "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n       101 site-PD1          running    6140             500.00 1\n      3001 site-srv19         running    4096              50.00 2\n"
            for v in self.vms:
                rows += "      %d rt-x               running    2048   10.00 3\n" % v
            for v in o.get("leftover", []):
                rows += "      %d rt-old             stopped    2048   10.00 0\n" % v
            return 0, rows, ""
        if c[:2] == ["pct", "list"]:
            return 0, "VMID       Status     Lock         Name\n3006       stopped                 site-srv3\n", ""
        if c[:2] == ["pvesm", "status"]:
            return 0, "Name Type Status Total Used Available %%\nlocal-lvm lvmthin active 3589201920 716045783 %d 19.95%%\n" % o.get("free_kib", 2873156136), ""
        if c[0] == "qmrestore":
            if o.get("restore_rc"):
                return o["restore_rc"], "", "erro de restore"
            self.vms[int(c[2])] = {"running": False}
            return 0, "", ""
        if c[:2] == ["qm", "config"]:
            v = int(c[2])
            conf = "agent: 1\nmemory: 8192\nname: orig\nscsi0: local-lvm:vm-%d-disk-0,size=50G\nide2: local:iso/x.iso,media=cdrom\n" % v
            if o.get("no_agent"):
                conf = conf.replace("agent: 1\n", "")
            if not self.nets_deleted or o.get("net_stays"):
                conf += "net0: virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0\nnet1: e1000=AA:BB:CC:DD:EE:02,bridge=vmbr0\n"
            if o.get("protected") and not self.protection0:
                conf += "protection: 1\n"                         # caso real da SAL-PD1 (pve3/101)
            if o.get("hostkeys"):                                  # caso real da AD1 (pve1/1003): hotplug de 10 CPUs + itens presos ao host
                for k, line in (("vcpus", "vcpus: 10"), ("numa0", "numa0: cpus=0-9,hostnodes=0,memory=4096,policy=bind"),
                                ("hookscript", "hookscript: local:snippets/hook.pl"), ("args", "args: -device foo"),
                                ("audio0", "audio0: device=ich9-intel-hda,driver=spice"), ("rng0", "rng0: source=/dev/urandom"),
                                ("affinity", "affinity: 0-3")):
                    if k not in self.deleted:
                        conf += line + "\n"
            return 0, conf, ""
        if c[:2] == ["qm", "set"]:
            if "--protection" in c and c[c.index("--protection") + 1] == "0":
                self.protection0 = True
            if "--delete" in c:
                keys = c[c.index("--delete") + 1].split(",")
                if o.get("delete_rc"):
                    return 1, "", o.get("delete_err", "erro de teste")
                if o.get("protected") and not self.protection0 and any(k.startswith(("ide", "sata", "scsi", "virtio")) for k in keys):
                    return 25, "", "VM 9000 is protected: can't remove drive 'ide2'"   # o Proxmox recusa e NADA e removido
                self.deleted |= set(keys)
                if any(k.startswith("net") for k in keys):
                    self.nets_deleted = True
            return 0, "", ""
        if c[:2] == ["qm", "start"]:
            if o.get("start_rc"):
                return 1, "kvm: motivo do qemu", "start falhou"
            if o.get("hostkeys") and "vcpus" not in self.deleted:
                return 1, "kvm: Invalid CPU topology: maxcpus (2) < smp_cpus (10)\n", "start failed: QEMU exited with code 1"
            self.vms[int(c[2])]["running"] = True
            return 0, "", ""
        if c[:2] == ["qm", "status"]:
            v = int(c[2])
            if v in self.vms:
                return 0, "status: %s\n" % ("running" if self.vms[v]["running"] and not o.get("dies") else "stopped"), ""
            return 2, "", "does not exist"
        if c[:2] == ["qm", "agent"]:
            return (0 if o.get("agent_ok", True) else 255), "", ""
        if c[:2] == ["qm", "stop"]:
            if int(c[2]) in self.vms:
                self.vms[int(c[2])]["running"] = False
            return 0, "", ""
        if c[:2] == ["qm", "destroy"]:
            if o.get("destroy_fails"):
                return 1, "", "x"
            self.vms.pop(int(c[2]), None)
            return 0, "", ""
        if c[0] == "pvesh" and c[1] == "get" and c[2].startswith("/nodes/") and c[2].endswith("/tasks"):    # trava de backup (v1.4)
            tk = o.get("tasks", [])
            return 0, json.dumps(tk() if callable(tk) else tk), ""
        if c[:3] == ["pvesh", "get", "/cluster/backup"]:
            jb = o.get("jobs", [])
            return 0, json.dumps(jb() if callable(jb) else jb), ""
        if c[0] == "systemd-analyze":
            txt = o["calendar"](c[-1]) if o.get("calendar") else None
            return (0, txt, "") if txt else (1, "", "Failed to parse calendar specification")
        if c[0] == "pgrep":
            return o.get("pgrep_rc", 1), "", ""
        if c[:3] == ["pvesh", "get", "/cluster/resources"]:
            return 0, json.dumps([{"vmid": v, "node": "outro-no"} for v in o.get("cluster_ids", [])] + [{"vmid": 101}, {"vmid": 3001}]), ""
        if c[0] == "pvesh":
            return 0, json.dumps({"diskread": o.get("diskread", 1.2e9)}), ""
        return 0, "", ""


def qm_vmids(r):
    """VMIDs que apareceram em comandos que mudam VMs (qm set/start/stop/destroy, qmrestore)."""
    out = set()
    for c in r.calls:
        if isinstance(c, tuple):
            continue
        if c[0] == "qmrestore":
            out.add(int(c[2]))
        elif c[0] == "qm" and c[1] in ("set", "start", "stop", "destroy"):
            out.add(int(c[2]))
    return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


LOG = []
log = LOG.append
E_QEMU = {"key": "pve1/1000", "host": "pve1", "vmid": "1000", "kind": "qemu", "fname": "vzdump-qemu-1000-2026_10_03-01_00_02.vma.zst",
          "path": "/m/pve1/dump/vzdump-qemu-1000-2026_10_03-01_00_02.vma.zst", "size": 5 * 2 ** 30, "ts": 1}
HEADER = "CFG: size: 500 name: qemu-server.conf\nDEV: dev_id=1 size: 53687091200 devname: drive-scsi0\nCTIME: x\n"


def pipe_header(cmds, cap):
    return ([0] * len(cmds), HEADER if cap else "", "", 1.0)


# ---- integridade: sucesso, falha de um estagio, timeout
fr0 = FakeRunner()
ok = t.verify_integrity(fr0, E_QEMU, CFG)
assert ok["ok"] and ok["msg"] == ""
assert all(c[:3] == ["nice", "-n", "19"] for c in fr0.calls[0][1]), "integridade tem que rodar com prioridade baixa"
bad = t.verify_integrity(FakeRunner(pipe=lambda c, cap: ([1, 0], "", "Data corruption detected", 2.0)), E_QEMU, CFG)
assert not bad["ok"] and "zstd" in bad["msg"] and "corruption" in bad["msg"]
to = t.verify_integrity(FakeRunner(pipe=lambda c, cap: ([124, 124], "", "timeout apos 9s", 9.0)), E_QEMU, CFG)
assert not to["ok"] and "tempo esgotado" in to["msg"]

# ---------------------------------------------------------------- seguranca do restore
for bad_id in (101, 3001, 8999, 9100, "9000", None):
    try:
        t.check_vmid(bad_id, CFG)
        raise SystemExit("check_vmid aceitou %r" % (bad_id,))
    except t.Unsafe:
        pass
assert t.check_vmid(9000, CFG) == 9000 and t.check_vmid(9099, CFG) == 9099
cfg_bad = t.json.loads(t.json.dumps(cfg_r))
cfg_bad["restore"].update({"vmid_min": 100, "vmid_max": 199})
fr = FakeRunner(pipe=pipe_header)
try:
    t.restore_test(fr, E_QEMU, cfg_bad, log)
    raise SystemExit("deveria abortar com faixa invalida")
except t.Unsafe:
    pass
assert not qm_vmids(fr), "nao pode mexer em nenhuma VM com faixa invalida"
try:
    t.cleanup_vm(FakeRunner(), 3001, CFG, log)
    raise SystemExit("cleanup aceitou VMID real")
except t.Unsafe:
    pass

# ---- restore OK com agent
clk = Clock()
fr = FakeRunner(pipe=pipe_header)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=clk.sleep, clock=clk)
assert res["ok"] and res["boot_check"] == "agent" and res["cleanup_ok"], res
assert qm_vmids(fr) == {9000}, qm_vmids(fr)
assert res["prov_gb"] == 50.0, res        # 53687091200 bytes no cabecalho do backup
assert fr.vms == {}, "VM de teste tem que ser destruida"
seq = [c[1] if c[0] == "qm" else c[0] for c in fr.calls if not isinstance(c, tuple)]
assert seq.index("qmrestore") < seq.index("start") < seq.index("destroy")
dele = [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "set"] and "--delete" in c][0]
assert set(dele[dele.index("--delete") + 1].split(",")) == {"net0", "net1", "ide2"}                                  # sem rede e sem cdrom
setc = [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "set"] and "--memory" in c][0]
assert setc[setc.index("--memory") + 1] == "2048" and "rt-1000" in setc and "--numa" in setc
assert "--protection" not in setc, "a protecao e desligada ANTES, numa chamada propria"
calls_set = [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "set"]]
assert calls_set[0][3:] == ["--protection", "0"], calls_set[0]
assert calls_set.index(dele) > 0, "a remocao de dispositivos vem depois de desligar a protecao"
assert setc[setc.index("--tags") + 1] == "restore-test;sem-backup", "VM de teste nasce isenta de backup (o coletor ignora)"
rest = [c for c in fr.calls if not isinstance(c, tuple) and c[0] == "qmrestore"][0]
assert rest[1] == E_QEMU["path"] and "--unique" in rest and rest[rest.index("--storage") + 1] == "local-lvm"

# ---- ID da VM de teste nunca conflita: ocupados no cluster (outros nos), VM e CT, ficam de fora; nunca usa --force
fr = FakeRunner(pipe=pipe_header, cluster_ids=[9000, 9001, 9005])
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert res["ok"] and qm_vmids(fr) == {9002}, qm_vmids(fr)
assert not [c for c in fr.calls if not isinstance(c, tuple) and c[0] == "qmrestore" and ("--force" in c or "-force" in c)]
fr = FakeRunner(pipe=pipe_header, cluster_ids=list(range(9000, 9100)))
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and "sem VMID livre" in res["msg"] and not qm_vmids(fr)

# ---- restore OK sem agent (heuristica de leitura de disco)
clk = Clock()
fr = FakeRunner(pipe=pipe_header, no_agent=True, diskread=1.2e9)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=clk.sleep, clock=clk)
assert res["ok"] and res["boot_check"] == "heuristica" and fr.vms == {}
# ---- sem sinal de boot: falha e limpa
clk = Clock()
fr = FakeRunner(pipe=pipe_header, no_agent=True, diskread=5e6)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=clk.sleep, clock=clk)
assert not res["ok"] and "sem sinal de boot" in res["msg"] and fr.vms == {} and res["cleanup_ok"]
# ---- VM desliga sozinha
clk = Clock()
fr = FakeRunner(pipe=pipe_header, dies=True)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=clk.sleep, clock=clk)
assert not res["ok"] and "desligou" in res["msg"] and fr.vms == {}
# ---- qmrestore falha: limpa mesmo assim, uma unica VM tocada
fr = FakeRunner(pipe=pipe_header, restore_rc=1)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and "qmrestore falhou" in res["msg"] and qm_vmids(fr) == {9000}
destroys = [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "destroy"]]
assert len(destroys) == 1
# ---- rede que nao saiu: nunca liga
fr = FakeRunner(pipe=pipe_header, net_stays=True)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and "placa de rede" in res["msg"]
assert not [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "start"]], "nao pode ligar com rede"
assert fr.vms == {}
# ---- start falha: a mensagem traz o motivo do QEMU (stdout) alem do stderr
fr = FakeRunner(pipe=pipe_header, start_rc=1)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and fr.vms == {} and "kvm: motivo do qemu" in res["msg"] and "start falhou" in res["msg"], res["msg"]
# ---- caso real SAL-PD1: VM com protection=1 e CD vazio (o Proxmox recusa remover drive de VM protegida; a rede sobrava)
fr = FakeRunner(pipe=pipe_header, protected=True)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert res["ok"] and fr.vms == {}, res
sets = [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "set"]]
assert sets[0][3:] == ["--protection", "0"] and fr.protection0 and fr.nets_deleted
# ---- caso real AD1: vcpus=10 (hotplug), NUMA, hookscript, args, audio, rng, afinidade removidos antes de subir com 2 nucleos
fr = FakeRunner(pipe=pipe_header, hostkeys=True)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert res["ok"] and fr.vms == {}, res
dele = [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "set"] and "--delete" in c][0]
assert {"vcpus", "numa0", "hookscript", "args", "audio0", "rng0", "affinity", "net0", "net1", "ide2"} <= set(dele[dele.index("--delete") + 1].split(","))
assert fr.deleted >= {"vcpus", "hookscript", "args"}
# ---- se a remocao falhar, a mensagem traz o erro do qm set (antes so dizia "ainda tem placa de rede")
fr = FakeRunner(pipe=pipe_header, delete_rc=1, delete_err="erro de teste", net_stays=True)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and "placa de rede" in res["msg"] and "erro de teste" in res["msg"], res["msg"]
assert not [c for c in fr.calls if not isinstance(c, tuple) and c[:2] == ["qm", "start"]], "nao pode ligar com rede"
assert fr.vms == {}
# ---- hostkeys que NAO sao removidas (remocao falhou): o QEMU recusa e a mensagem explica o porque
fr = FakeRunner(pipe=pipe_header, hostkeys=True, delete_rc=1)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and fr.vms == {}
# ---- limpeza que falha e sinalizada
fr = FakeRunner(pipe=pipe_header, destroy_fails=True)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert res["ok"] and res["cleanup_ok"] is False and "NAO removida" in res["msg"]
# ---- erro inesperado no meio: limpa
class Boom(FakeRunner):
    def run(self, cmd, timeout=120):
        if cmd[:2] == ["qm", "start"]:
            raise RuntimeError("explodiu")
        return FakeRunner.run(self, cmd, timeout)


fr = Boom(pipe=pipe_header)
res = t.restore_test(fr, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and "explodiu" in res["msg"] and fr.vms == {}
# ---- recusas: disco grande, CT, pouco espaco, tamanho ilegivel -> nada e criado
big = lambda c, cap: ([0] * len(c), "DEV: dev_id=1 size: %d devname: d\n" % (1200 * 2 ** 30), "", 1.0)
for runner, expect in ((FakeRunner(pipe=big), "acima do limite"), (FakeRunner(pipe=pipe_header, free_kib=10 * 1048576), "espaco insuficiente"),
                       (FakeRunner(pipe=lambda c, cap: ([0] * len(c), "", "", 1.0)), "tamanho dos discos")):
    res = t.restore_test(runner, E_QEMU, cfg_r, log, sleep=Clock().sleep, clock=Clock())
    assert not res["ok"] and expect in res["msg"] and not qm_vmids(runner), (expect, res)
    assert res.get("skip") or "espaco" in res["msg"]
ct = dict(E_QEMU, kind="lxc", fname="vzdump-lxc-3006-x.tar.zst", path="/m/x.tar.zst")
res = t.restore_test(FakeRunner(pipe=pipe_header), ct, cfg_r, log, sleep=Clock().sleep, clock=Clock())
assert not res["ok"] and res.get("skip") == "ct"
# ---- VM de teste que sobrou de uma execucao interrompida e removida; VM real nunca
fr = FakeRunner(leftover=[9005])
fr.vms[9005] = {"running": False}
assert t.sweep_leftovers(fr, cfg_r, log) == 0 and 9005 not in fr.vms
assert qm_vmids(fr) == {9005}, "o sweep so pode tocar na VM de teste"

# ---------------------------------------------------------------- registro e relatorio
s = t.new_state()
entry = by["pve1/1000"]
t.touch_inventory(s, inv, NOW)
assert s["vms"]["pve1/1000"]["first_seen"] == int(NOW)
t.record_result(s, entry, "integrity", {"ok": True, "secs": 12.0, "msg": ""}, NOW)
t.record_result(s, by["pve2/150"], "integrity", {"ok": False, "secs": 3.0, "msg": "falha em zstd: corrompido"}, NOW)
assert t.record_result(s, by["pve3/101"], "restore", {"ok": False, "skip": "grande", "msg": "x"}, NOW) is None
assert s["vms"]["pve3/101"]["restore_skip"] == "grande"
t.record_result(s, by["pve3/3015"], "restore", {"ok": True, "secs": 600.0, "boot_check": "agent", "msg": "boot"}, NOW)
assert s["vms"]["pve3/3015"]["integrity"]["ok"], "restore OK tambem vale como integridade"
rep = json.loads(json.dumps(t.build_report(inv, s, CFG, NOW + 3600, "pve3")))
sm = rep["summary"]
tests = dict((x["key"], x) for x in rep["tests"])
assert tests["pve1/1000"]["last_result"] == "ok" and tests["pve1/1000"]["last_ok_ts"] == int(NOW)
assert tests["pve2/150"]["last_result"] == "failed" and tests["pve2/150"]["last_ok_ts"] is None
assert tests["pve1/1002"]["skip"] == "nao-marcada" and tests["pve1/1004"]["skip"] == "isento" and not tests["pve1/1002"]["eligible"]
assert tests["pve3/3006"]["last_result"] == "none"
assert sm["failed"] == 1 and sm["never"] == 3 and sm["tested_ok"] == 2, sm          # 1000, 3015 ok; 150 falhou; 101/3006/102 nunca
assert sm["skipped"] == 5 and sm["restore_enabled"] == 0 and sm["cleanup_pending"] == 0
assert all(v is not None for v in sm.values()), "summary nao pode ter nulos (Zabbix)"
late = t.build_report(inv, s, CFG, NOW + 36 * 86400, "pve3")["summary"]
assert late["overdue"] >= 3                                                           # nunca testadas ha > 35 dias + testes velhos

# ---------------------------------------------------------------- execucao completa (montagem simulada)
def fake_mount(runner, cfg, log):
    return {"pve1": src1, "pve3": src3, "pve2": src2}, {}


work = tempfile.mkdtemp()
paths = dict(state_path=os.path.join(work, "state.json"), inv_path=os.path.join(work, "inv.json"),
             out=os.path.join(work, "tests.json"), hist=os.path.join(work, "hist.jsonl"))
LOG.clear()
runner = FakeRunner()
n = t.run_tests(runner, CFG, log, now_fn=lambda: NOW, sleep=lambda s: None, mount=fake_mount, unmount=lambda r, c: None, **paths)
assert n == 6, n                                                                       # todas as devidas, uma por vez
pipes = [c for c in runner.calls if isinstance(c, tuple)]
assert len(pipes) == 6
st_saved = json.load(open(paths["state_path"]))
assert st_saved["last_run"]["result"] == "ok" and all(v["integrity"]["ok"] for k, v in st_saved["vms"].items() if "integrity" in v)
assert not (set(st_saved["vms"]) & {"pve1/1001", "pve1/1002", "pve1/1004", "pve1/1007", "pve3/3999"}) or all(
    "integrity" not in st_saved["vms"][k] for k in ("pve1/1001", "pve1/1002", "pve1/1004", "pve1/1007", "pve3/3999") if k in st_saved["vms"])
out = json.load(open(paths["out"]))
assert out["summary"]["never"] == 0 and out["summary"]["tested_ok"] == 6 and out["summary"]["last_run_result"] == "ok"
assert len(open(paths["hist"]).read().strip().splitlines()) == 6
# segunda rodada: nada devido
runner = FakeRunner()
assert t.run_tests(runner, CFG, log, now_fn=lambda: NOW + 60, sleep=lambda s: None, mount=fake_mount, unmount=lambda r, c: None, **paths) == 0
# fora da janela: nada
LOG.clear()
os.remove(paths["state_path"])
noon = datetime.datetime(2026, 10, 6, 12, 0).timestamp()
assert t.run_tests(FakeRunner(), CFG, log, now_fn=lambda: noon, sleep=lambda s: None, mount=fake_mount, unmount=lambda r, c: None, **paths) == 0
assert any("fora da janela" in m for m in LOG)
# manual: --only ignora a janela com --force-window e testa so aquela VM
os.remove(paths["state_path"])
runner = FakeRunner()
assert t.run_tests(runner, CFG, log, now_fn=lambda: noon, only="pve3/101", level="integrity", force_window=True, sleep=lambda s: None,
                   mount=fake_mount, unmount=lambda r, c: None, **paths) == 1
assert len([c for c in runner.calls if isinstance(c, tuple)]) == 1 and "101" in json.dumps([c for c in runner.calls if isinstance(c, tuple)][-1])
# falha de integridade vira estado 'failed' e entra no relatorio
os.remove(paths["state_path"])
bad_runner = FakeRunner(pipe=lambda c, cap: ([0 if "vma" in c[0] else 1] * len(c), "", "erro de leitura", 1.0))
t.run_tests(bad_runner, CFG, log, now_fn=lambda: NOW, sleep=lambda s: None, mount=fake_mount, unmount=lambda r, c: None, **paths)
rep = json.load(open(paths["out"]))
assert rep["summary"]["failed"] >= 1 and rep["summary"]["last_run_result"] == "failed"
# oscilacao do NAS: falha na 1a leitura, ok na 2a -> registra OK (sem alarme falso)
os.remove(paths["state_path"])
calls = {"n": 0}


def flaky(c, cap):
    calls["n"] += 1
    return ([1, 1] if calls["n"] == 1 else [0] * len(c), "", "Input/output error", 1.0)


LOG.clear()
slept = []
fl = FakeRunner(pipe=flaky)
t.run_tests(fl, CFG, log, now_fn=lambda: NOW, sleep=slept.append, mount=fake_mount, unmount=lambda r, c: None, **paths)
first = json.load(open(paths["state_path"]))
assert 60 in slept and sum(1 for v in first["vms"].values() if v.get("integrity", {}).get("ok")) == 6
assert any("segunda leitura" in m for m in LOG)
assert json.load(open(paths["out"]))["summary"]["failed"] == 0 and json.load(open(paths["out"]))["summary"]["last_run_result"] == "ok"
# falha confirmada nas duas leituras -> falhou (e so uma retentativa por VM)
os.remove(paths["state_path"])
always = FakeRunner(pipe=lambda c, cap: ([1] * len(c), "", "corrompido", 1.0))
t.run_tests(always, CFG, log, now_fn=lambda: NOW, sleep=lambda s: None, mount=fake_mount, unmount=lambda r, c: None, **paths)
assert len([c for c in always.calls if isinstance(c, tuple)]) == 12 and json.load(open(paths["out"]))["summary"]["failed"] == 6
# formato desconhecido nao e retentado
unk = t.verify_integrity(FakeRunner(), dict(E_QEMU, path="/m/x.iso"), CFG)
assert not unk["ok"] and unk.get("final")
# refresh: so regrava a partir do estado salvo
os.remove(paths["out"])
t.refresh(CFG, now=NOW, state_path=paths["state_path"], inv_path=paths["inv_path"], out=paths["out"])
assert json.load(open(paths["out"]))["summary"]["failed"] >= 1
# janela estourada no meio: teste grande nao comeca se nao cabe
LOG.clear()
os.remove(paths["state_path"])
near_end = datetime.datetime(2026, 10, 7, 5, 50).timestamp()                           # 10 min para o fim
huge = make_source("huge", [("vzdump-qemu-777-2026_10_04-01_00_00.vma.zst", 1)])
inv_h = [dict(t.build_inventory({"H": huge}, cfg_all, NOW)[0], size=800 * 2 ** 30)]
assert t.plan_tests(inv_h, t.new_state(), cfg_all, NOW)[0]["entry"]["size"] == 800 * 2 ** 30
assert t.estimate_seconds(t.plan_tests(inv_h, t.new_state(), cfg_all, NOW)[0], cfg_all) > 600

# ---------------------------------------------------------------- storage.cfg e relatorio status
real_cfg = """dir: local
\tpath /var/lib/vz
\tcontent iso,vztmpl,backup

cifs: nas-backup
\tpath /mnt/pve/nas-backup
\tserver 192.0.2.246
\tshare PROXMOX
\tcontent backup
\tprune-backups keep-last=2,keep-weekly=2
\tsubdir /pve3
\tusername proxmox
"""
sc = t.parse_storage_cfg(real_cfg, "nas-backup")
assert sc["server"] == "192.0.2.246" and sc["share"] == "PROXMOX" and sc["username"] == "proxmox" and sc["subdir"] == "/pve3"
assert t.parse_storage_cfg(real_cfg, "local") == {}
assert "*/5 * * * *" in t.CRON_TEXT and "0 23 * * 0-4" in t.CRON_TEXT and "flock -n" in t.CRON_TEXT
# ---------------------------------------------------------------- simulacao de noites (--plan): nunca promete mais do que cabe na janela
assert t.window_seconds(CFG["window"]) == 7 * 3600


def item(size_gb, level="integrity"):
    return {"entry": {"size": size_gb * 2 ** 30, "key": "h/%d" % size_gb}, "level": level, "reason": ""}


nights, stuck = t.pack_nights([item(500)] * 6, CFG, 7 * 3600)
assert [len(n) for n in nights] == [3, 3] and not stuck                      # 500 GB ~1,8 h cada: 3 por noite
nights, stuck = t.pack_nights([item(100)] * 17, CFG, 7 * 3600)
assert [len(n) for n in nights] == [17] and not stuck
nights, stuck = t.pack_nights([item(3000), item(10)], CFG, 7 * 3600)
assert [len(n) for n in nights] == [1] and len(stuck) == 1                   # backup que nao cabe nem numa noite inteira
cfg_r1 = t.json.loads(t.json.dumps(cfg_r))
cfg_r1["restore"]["max_per_night"] = 1
nights, stuck = t.pack_nights([item(10, "restore"), item(10, "restore")], cfg_r1, 7 * 3600)
assert [p["level"] for p in nights[0]] == ["restore", "integrity"]           # no maximo 1 restore por noite

# ---------------------------------------------------------------- simulacao de ~2 semanas com a politica nova (diaria todo dia, semanal 1x, grande 1x por semana)
def epoch(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi).timestamp()


sim_root = os.path.join(tmp, "sim")
os.makedirs(os.path.join(sim_root, "dump"))
sim_made = set()


def sim_backup(vmid, when):
    fn = "vzdump-qemu-%s-%s.vma.zst" % (vmid, datetime.datetime.fromtimestamp(when).strftime("%Y_%m_%d-%H_%M_%S"))
    pth = os.path.join(sim_root, "dump", fn)
    with open(pth, "wb") as f:
        f.write(b"x" * 1000)
    os.utime(pth, (when, when))


events = []                                       # (quando, vmid)
for day in range(10, 23):                          # diaria: todo dia 21:00
    events.append((epoch(2026, 10, day, 21, 0), 5001))
for day in (10, 17):                               # semanal e grande: sabado 01:00
    events += [(epoch(2026, 10, day, 1, 0), 5002), (epoch(2026, 10, day, 1, 0), 5003)]
HDR50 = "DEV: dev_id=1 size: %d devname: d\n" % (50 * 2 ** 30)
HDR300 = "DEV: dev_id=1 size: %d devname: d\n" % (300 * 2 ** 30)


def sim_pipe(cmds, cap):
    return ([0] * len(cmds), (HDR300 if "-5003-" in json.dumps(cmds) else HDR50) if cap else "", "", 1.0)


cfg_s = t.json.loads(t.json.dumps(CFG))
cfg_s["restore"]["enabled"] = True
sim_paths = dict(state_path=os.path.join(tmp, "sim_state.json"), inv_path=os.path.join(tmp, "sim_inv.json"),
                 out=os.path.join(tmp, "sim_tests.json"), hist=os.path.join(tmp, "sim_hist.jsonl"))
sim_runner = FakeRunner(pipe=sim_pipe)
nights = [(11, "dom"), (12, "seg"), (13, "ter"), (14, "qua"), (15, "qui"), (18, "dom"), (19, "seg")]
expected = {11: {5001: 1, 5002: 1, 5003: 1}, 12: {5001: 1}, 13: {5001: 1}, 14: {5001: 1}, 15: {5001: 1},
            18: {5001: 1, 5002: 1, 5003: 1}, 19: {5001: 1}}
for day, nome in nights:
    run_at = epoch(2026, 10, day, 23, 0)
    for when, vmid in events:
        if when <= run_at and (when, vmid) not in sim_made:
            sim_backup(vmid, when)
            sim_made.add((when, vmid))
    src_sim = {"dump": os.path.join(sim_root, "dump"), "root": sim_root}
    json.dump({"generated": run_at - 60, "guests": [G(5001, "DIARIA"), G(5002, "SEMANAL"), G(5003, "GRANDE")]},
              open(os.path.join(sim_root, CFG["status_file"]), "w"))
    before = len(sim_runner.calls)
    t.run_tests(sim_runner, cfg_s, log, now_fn=lambda: run_at, sleep=lambda s: None,
                mount=lambda r, c, l: ({"H": src_sim}, {}), unmount=lambda r, c: None, **sim_paths)
    got = {}
    for c in sim_runner.calls[before:]:
        if not isinstance(c, tuple) and c[0] == "qmrestore":
            vm = int(c[1].split("vzdump-qemu-")[1].split("-")[0])
            got[vm] = got.get(vm, 0) + 1
    assert got == expected[day], (nome, day, got, expected[day])
    assert not sim_runner.vms, "nenhuma VM de teste pode sobrar"
# nas noites sem janela (sexta e sabado) nada roda
fri = epoch(2026, 10, 16, 23, 0)
before = len(sim_runner.calls)
t.run_tests(sim_runner, cfg_s, log, now_fn=lambda: fri, sleep=lambda s: None,
            mount=lambda r, c, l: ({"H": src_sim}, {}), unmount=lambda r, c: None, **sim_paths)
assert not [c for c in sim_runner.calls[before:] if not isinstance(c, tuple) and c[0] == "qmrestore"]
sim_state = json.load(open(sim_paths["state_path"]))
assert sim_state["vms"]["H/5003"]["prov_gb"] == 300.0                     # VM grande: tamanho guardado para a regra semanal
assert json.load(open(sim_paths["out"]))["summary"]["failed"] == 0

# estimativa do restore: .vma sem compressao nao "triplica" o tamanho; com o provisionado conhecido, usa ele
big_vma = {"entry": {"size": 241 * 2 ** 30, "key": "h/3015", "fname": "vzdump-qemu-3015-x.vma"}, "level": "restore", "reason": ""}
est_h = t.estimate_seconds(big_vma, CFG) / 3600.0
assert 1.0 < est_h < 2.5, est_h
assert t.estimate_seconds(dict(big_vma, prov_gb=300.0), CFG) < t.estimate_seconds(dict(big_vma, prov_gb=900.0), CFG)
small = {"entry": {"size": 5 * 2 ** 30, "key": "h/1", "fname": "vzdump-qemu-1-x.vma.zst"}, "level": "restore", "reason": ""}
assert t.estimate_seconds(small, CFG) < 1200

# ---------------------------------------------------------------- montagem: arquivo de senha do Proxmox ('password=SENHA')
cfgdir = tempfile.mkdtemp()
open(os.path.join(cfgdir, "storage.cfg"), "w").write(real_cfg.replace("\tusername proxmox\n", "\tusername proxmox\n\tdomain EMB\n\tsmbversion 3.0\n"))
t.PVE_STORAGE_CFG = os.path.join(cfgdir, "storage.cfg")
t.PVE_PRIV = os.path.join(cfgdir, "%s.pw")
t.CRED = os.path.join(cfgdir, "cred")
t.MOUNT_BASE = os.path.join(cfgdir, "mnt")


class MountSpy(FakeRunner):
    def run(self, cmd, timeout=120):
        if cmd[0] == "mount":
            self.cred = open(t.CRED).read()
            self.opts = cmd[cmd.index("-o") + 1]
            self.src = getattr(self, 'src', None) or cmd[3]   # primeira fonte (pve1)
            return 13, "", "mount error(13): Permission denied\nRefer to the mount.cifs(8) manual page"
        return FakeRunner.run(self, cmd, timeout)


for content in ("password=S3nh@,com=sinais\n", "S3nh@,com=sinais\n"):          # formato do Proxmox e senha pura
    open(t.PVE_PRIV % "nas-backup", "w").write(content)
    ms = MountSpy()
    srcs_m, errs_m = t.mount_sources(ms, CFG, log)
    assert ms.cred == "username=proxmox\npassword=S3nh@,com=sinais\ndomain=EMB\n", ms.cred
    assert "ro," in ms.opts and "vers=3.0" in ms.opts and ms.src == "//192.0.2.246/PROXMOX/pve1"
    assert not srcs_m and "usuario=proxmox dominio=EMB smb=3.0" in errs_m["pve1"] and "Permission denied" in errs_m["pve1"]
    assert not os.path.exists(t.CRED), "arquivo de credenciais tem que ser apagado"

# ---------------------------------------------------------------- trava de backup em andamento (v1.4)
assert CFG["guard"]["enabled"] and CFG["guard"]["margin_min"] == 20 and CFG["guard"]["poll_seconds"] == 120


def ts(mo, d, h, mi, y=2026):
    return datetime.datetime(y, mo, d, h, mi).timestamp()


class Clk2:
    """Relogio simulado: sleep() avanca o tempo."""
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def cal_for(clock, table):
    """Simula 'systemd-analyze calendar': devolve a primeira data da tabela depois de 'agora' no formato real."""
    def fn(sched):
        for e in table.get(sched, []):
            if e > clock.t:
                return "  Original form: %s\nNormalized form: x\n    Next elapse: %s\n       (in UTC): y\n" % (
                    sched, datetime.datetime.fromtimestamp(e).strftime("%a %Y-%m-%d %H:%M:%S -03"))
        return None
    return fn


GCFG = t.json.loads(t.json.dumps(CFG))
SRC_G = make_source("pxg", [("vzdump-qemu-150-2026_10_06-21_00_00.vma.zst", 1000)])

# 1) backup_running: vzdump ativo, outra tarefa, pvesh fora do ar (plano B), arquivo parcial no NAS
assert t.backup_running(FakeRunner(), GCFG, "pve3", {"pve3": SRC_G}, NOW) == ""
rr = t.backup_running(FakeRunner(tasks=[{"type": "vzdump", "id": "3015"}, {"type": "qmstart", "id": "101"}]), GCFG, "pve3", {}, NOW)
assert "vzdump" in rr and "3015" in rr
assert "varias VMs" in t.backup_running(FakeRunner(tasks=[{"type": "vzdump", "id": ""}]), GCFG, "pve3", {}, NOW)
assert t.backup_running(FakeRunner(tasks=[{"type": "qmstart", "id": "1"}, {"type": "qmrestore", "id": "9000"}]), GCFG, "pve3", {}, NOW) == ""


class PveshDown(FakeRunner):
    def run(self, cmd, timeout=120):
        if cmd[0] == "pvesh":
            self.calls.append(list(cmd))
            return 1, "", "no quorum"
        return FakeRunner.run(self, cmd, timeout)


assert t.backup_running(PveshDown(pgrep_rc=0), GCFG, "pve3", {}, NOW) != ""
assert t.backup_running(PveshDown(pgrep_rc=1), GCFG, "pve3", {}, NOW) == ""
P_DAT = os.path.join(SRC_G["dump"], "vzdump-qemu-101-2026_10_06-21_30_00.vma.dat")
open(P_DAT, "wb").write(b"x")
os.utime(P_DAT, (NOW - 120, NOW - 120))
assert "NAS" in t.backup_running(FakeRunner(), GCFG, "pve3", {"pve2": SRC_G}, NOW)
os.utime(P_DAT, (NOW - 3 * 3600, NOW - 3 * 3600))
assert t.backup_running(FakeRunner(), GCFG, "pve3", {"pve2": SRC_G}, NOW) == "", "arquivo parcial abandonado nao pode travar para sempre"
os.remove(P_DAT)

# 2) jobs_next: ativo/desativado, outro no, formato antigo (dow+starttime), agenda ilegivel (avisa uma vez)
clk = Clk2(ts(11, 30, 23, 30))
TABLE = {"*-*-01 02:00": [ts(12, 1, 2, 0), ts(1, 1, 2, 0, 2027)], "mon,tue 21:00": [ts(12, 1, 21, 0)], "sat 01:00": [ts(12, 5, 1, 0)]}
JOBS = [{"id": "backup-m", "schedule": "*-*-01 02:00", "enabled": 1},
        {"id": "backup-off", "schedule": "sat 01:00", "enabled": 0},
        {"id": "backup-outro-no", "schedule": "sat 01:00", "enabled": 1, "node": "outro"},
        {"id": "backup-velho", "starttime": "21:00", "dow": "mon,tue", "enabled": 1},
        {"id": "backup-ruim", "schedule": "isso nao e agenda", "enabled": 1}]
jr = FakeRunner(jobs=JOBS, calendar=cal_for(clk, TABLE))
lg_j = []
res_j = t.jobs_next(jr, "pve3", clk.t, lg_j.append)
assert [d for _, d in res_j] == ["job backup-m (*-*-01 02:00)", "job backup-velho (mon,tue 21:00)"], res_j
assert res_j[0][0] == ts(12, 1, 2, 0)
assert any("nao consegui interpretar" in m and "isso nao e agenda" in m for m in lg_j)
lg_j2 = []
t.jobs_next(jr, "pve3", clk.t, lg_j2.append)
assert not lg_j2, "o aviso de agenda ilegivel nao pode repetir a cada consulta"

# 3) guard_wait
clk = Clk2(T0 := ts(11, 30, 23, 30))
lg = []
go, nxt, desc = t.guard_wait(FakeRunner(), GCFG, lg.append, "pve3", {}, clk, clk.sleep, {"hold": 0}, True)
assert go and nxt is None and clk.t == T0 and not lg                                # livre: nao espera nada
clk = Clk2(T0)
lg = []
go, _, _ = t.guard_wait(FakeRunner(tasks=lambda: [{"type": "vzdump", "id": "3015"}] if clk.t < T0 + 600 else []),
                        GCFG, lg.append, "pve3", {}, clk, clk.sleep, {"hold": 0}, True)
assert go and T0 + 600 <= clk.t <= T0 + 720 and any("pausa" in m for m in lg) and any("liberado" in m for m in lg), (clk.t - T0, lg)
clk = Clk2(T0)
lg = []
go, _, _ = t.guard_wait(FakeRunner(tasks=[{"type": "vzdump", "id": ""}]), GCFG, lg.append, "pve3", {}, clk, clk.sleep, {"hold": 0}, True)
assert not go and clk.t - T0 >= 300 * 60 and any("espera passou" in m for m in lg)   # backup sem fim: desiste depois de 5 h
clk = Clk2(ts(12, 1, 5, 50))
lg = []
go, _, _ = t.guard_wait(FakeRunner(tasks=[{"type": "vzdump", "id": "1"}]), GCFG, lg.append, "pve3", {}, clk, clk.sleep, {"hold": 0}, False)
assert not go and any("janela esta acabando" in m for m in lg)                      # 10 min para o fim da janela: nao fica esperando
T_JOB, T_END = ts(12, 1, 2, 0), ts(12, 1, 2, 40)
clk = Clk2(ts(12, 1, 1, 45))
lg = []
jr = FakeRunner(jobs=[{"id": "backup-m", "schedule": "*-*-01 02:00", "enabled": 1}], calendar=cal_for(clk, TABLE),
                tasks=lambda: [{"type": "vzdump", "id": "3015"}] if T_JOB <= clk.t < T_END else [])
go, nxt, desc = t.guard_wait(jr, GCFG, lg.append, "pve3", {}, clk, clk.sleep, {"hold": 0}, True)
assert go and T_END <= clk.t <= T_END + 120 and "comeca em" in lg[0], (clk.t, lg)  # job as 02:00: espera comecar, rodar e acabar
clk = Clk2(ts(12, 1, 0, 0))
go, nxt, desc = t.guard_wait(FakeRunner(jobs=[{"id": "backup-m", "schedule": "*-*-01 02:00", "enabled": 1}], calendar=cal_for(clk, TABLE)),
                             GCFG, lg.append, "pve3", {}, clk, clk.sleep, {"hold": 0}, True)
assert go and nxt == T_JOB and clk.t == ts(12, 1, 0, 0)                              # job so daqui a 2 h: segue e devolve a hora

# 4) run_tests de ponta a ponta com a trava


def fresh_state():
    if os.path.exists(paths["state_path"]):
        os.remove(paths["state_path"])


fresh_state()
clk = Clk2(NOW)
LOG.clear()
starts = []
busy_until = NOW + 1200
r1 = FakeRunner(pipe=lambda c, cap: (starts.append(clk.t), ([0] * len(c), "", "", 1.0))[1],
                tasks=lambda: [{"type": "vzdump", "id": "150"}] if clk.t < busy_until else [])
n1 = t.run_tests(r1, GCFG, log, now_fn=clk, sleep=clk.sleep, mount=fake_mount, unmount=lambda r_, c_: None, **paths)
assert n1 == 6 and min(starts) >= busy_until and any("guarda" in m for m in LOG), (n1, starts)   # nada testado durante o backup

fresh_state()
clk = Clk2(NOW)
LOG.clear()
starts = []
gc = t.json.loads(t.json.dumps(GCFG))
gc["guard"]["margin_min"] = 5
t_j = NOW + 360                                         # job comeca daqui a 6 min e roda ate NOW+1200
tbl = {"x": [t_j, NOW + 30 * 86400]}
r2 = FakeRunner(pipe=lambda c, cap: (starts.append(clk.t), ([0] * len(c), "", "", 1.0))[1],
                jobs=[{"id": "backup-n", "schedule": "x", "enabled": 1}], calendar=cal_for(clk, tbl),
                tasks=lambda: [{"type": "vzdump", "id": ""}] if t_j <= clk.t < NOW + 1200 else [])
n2 = t.run_tests(r2, gc, log, now_fn=clk, sleep=clk.sleep, mount=fake_mount, unmount=lambda r_, c_: None, **paths)
assert n2 == 6 and min(starts) >= NOW + 1200, (n2, [s_ - NOW for s_ in starts])   # nenhum teste cabia antes do job: esperou o job passar
assert any("nao cabem antes do" in m for m in LOG)

fresh_state()                                           # trava desligada (--sem-guarda ou guard.enabled=false): ignora backup rodando
for kwargs, cfg_x in ((dict(use_guard=False), GCFG), (dict(), dict(GCFG, guard=dict(GCFG["guard"], enabled=False)))):
    fresh_state()
    r3 = FakeRunner(tasks=[{"type": "vzdump", "id": "1"}])
    assert t.run_tests(r3, cfg_x, log, now_fn=lambda: NOW, sleep=lambda s: None, mount=fake_mount, unmount=lambda r_, c_: None, **paths, **kwargs) == 6
    assert not any(isinstance(c, list) and c[:2] == ["pvesh", "get"] and "tasks" in c[2] for c in r3.calls), "trava desligada nao pode consultar tarefas"

# 5) --check mostra o que a trava enxerga
clk = Clk2(ts(11, 30, 23, 30))
rep = "\n".join(t.guard_report(FakeRunner(jobs=JOBS[:1], calendar=cal_for(clk, TABLE), tasks=[{"type": "vzdump", "id": "1"}]), GCFG, "pve3", clk.t))
assert "das quais 1 de backup" in rep and "01/12/2026 02:00" in rep and "20 min" in rep, rep
assert "DESLIGADA" in t.guard_report(FakeRunner(), dict(GCFG, guard=dict(GCFG["guard"], enabled=False)), "pve3", clk.t)[0]
print("trava de backup: OK")

t.print_status(inv, s)
print("TODOS OS TESTES DO TESTADOR PASSARAM")
