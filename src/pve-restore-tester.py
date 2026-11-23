#!/usr/bin/env python3
"""pve-restore-tester.py (v1.4) - testa automaticamente os backups do NAS.

Roda no pve3. Monta os backups dos Proxmox (pve1, pve2, pve3) SOMENTE LEITURA, descobre sozinho
cada VM que tem backup e testa o backup mais recente de cada uma:
  integridade : le o arquivo inteiro (zstd/lzop/gzip + vma verify, ou tar -t em CT)
  restore     : restaura numa VM temporaria (VMID 9000-9099), SEM rede, com RAM limitada, sobe e
                confere o boot; depois destroi tudo. Vem DESLIGADO na rotina noturna ate ser ligado
                na configuracao (teste manual de uma VM: --only HOST/VMID --level restore).
Quem e testado (por TAG na VM, no Proxmox):
  testar           -> a VM e testada (so entram as VMs que tem esta tag E tem backup)
  sem tag 'testar' -> NAO testada
  sem-backup       -> NAO testada (VM isenta de backup), mesmo com a tag 'testar'
  (opcional) "require_tag": "" na configuracao -> testa toda VM que tem backup, sem precisar de tag
As tags vem da copia do estado de cada host no NAS (pve-backup-status.json).
VM de teste: sempre VMID 9000-9099 (livre em TODO o cluster), nome rt-<vmid>, SEM placa de rede
(protecao desligada, CPU hotplug/NUMA/afinidade/hookscript/args removidos antes de ligar).
Escolhe sozinho a VM mais atrasada (nunca testadas primeiro) e so trabalha na janela configurada.
Trava de backup (v1.4): nao comeca teste enquanto ha backup em andamento (vzdump neste no, ou arquivo parcial sendo gravado no
NAS) nem quando um job de backup deste no comecaria antes do teste terminar. Espera o backup passar e continua na mesma noite.
Resultado em /var/lib/pve-backup-monitor/tests.json (o agent2 do Zabbix le este arquivo).

Uso:
  --check                       verifica ferramentas, montagem do NAS, tags e listagem (nao testa nada)
  --plan                        mostra o que seria testado agora (nao testa nada)
  --status                      tabela do ultimo teste de cada VM
  --run                         testa o que estiver devido, dentro da janela (cron)
  --run --only HOST/VMID --level integrity|restore [--force-window] [--sem-guarda]   teste manual de uma VM
                                (--sem-guarda ignora a trava de backup em andamento)
  --refresh                     regrava tests.json a partir do estado (cron a cada 5 min)
  --install                     cria pastas, configuracao padrao e agendamento no cron
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin:" + os.environ.get("PATH", "")

VERSION = 1
CONF = "/etc/pve-restore-tester.json"
STATE_DIR = "/var/lib/pve-restore-tester"
STATE = STATE_DIR + "/state.json"
INVENTORY = STATE_DIR + "/inventory.json"
HISTORY = STATE_DIR + "/history.jsonl"
OUT = "/var/lib/pve-backup-monitor/tests.json"
LOG = "/var/log/pve-restore-tester.log"
LOCK = "/run/pve-restore-tester.lock"
MOUNT_BASE = "/mnt/restore-tester"
CRED = "/run/pve-restore-tester.cred"
CRON = "/etc/cron.d/pve-restore-tester"
PVE_STORAGE_CFG = "/etc/pve/storage.cfg"
PVE_PRIV = "/etc/pve/priv/storage/%s.pw"

DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
TOLERANCE_DAYS = 0.5   # o teste de ontem pode ter rodado mais tarde que o de hoje: meio dia de folga nos intervalos minimos

DEFAULTS = {
    "storage_id": "nas-backup",
    "sources": [{"host": "pve1", "subdir": "pve1"},
                {"host": "pve2", "subdir": "pve2"},
                {"host": "pve3", "subdir": "pve3"}],
    "window": {"days": ["sun", "mon", "tue", "wed", "thu"], "start": "23:00", "end": "06:00"},
    "require_tag": "testar",
    "status_file": "pve-backup-status.json",
    "status_max_age_hours": 24,
    "integrity_every_days": 1,      # backup novo: verifica na mesma noite (diarias todo dia, semanais 1x por semana)
    "integrity_max_days": 30,
    "restore_min_days": 1,          # restore a cada backup novo, no minimo 1 dia entre dois restores da mesma VM
    "restore_big_gb": 200,          # disco provisionado acima disso = VM grande
    "restore_big_min_days": 7,      # VM grande: no maximo 1 restore por semana
    "restore_retry_days": 1,        # restore que falhou: tenta de novo na noite seguinte
    "restore_max_gb": 1000,         # acima disso so integridade (protege o espaco do storage de destino)
    "max_backup_age_days": 40,
    "overdue_days": 35,
    "nas_read_mb_s": 80,
    "guard": {
        "enabled": True,
        "margin_min": 20,           # nao comeca teste que acabaria menos de 20 min antes de um job de backup deste no
        "poll_seconds": 120,        # enquanto espera o backup acabar, olha de novo a cada 2 min
        "max_wait_min": 300,        # espera no maximo 5 h por vez (a janela de testes tambem limita)
        "partial_minutes": 30,      # arquivo parcial (.dat/.tmp) do NAS mexido ha menos que isso = backup sendo gravado
    },
    "restore": {
        "enabled": False,
        "vmid_min": 9000, "vmid_max": 9099,
        "memory_mb": 2048, "cores": 2,
        "target_storage": "local-lvm",
        "boot_timeout_min": 10, "boot_min_seconds": 180, "boot_min_diskread_mb": 300,
        "max_per_night": 25, "free_factor": 1.25,
    },
}

FILE_RE = re.compile(r"^vzdump-(qemu|lxc|openvz)-(\d+)-(\d{4})_(\d{2})_(\d{2})-(\d{2})_(\d{2})_(\d{2})"
                     r"\.(vma|tar)(\.zst|\.lzo|\.gz)?$")


# ------------------------------------------------------------------ utilidades
def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_conf(path=CONF):
    try:
        with open(path) as f:
            return deep_merge(DEFAULTS, json.load(f))
    except IOError:
        return json.loads(json.dumps(DEFAULTS))


def atomic_write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, ValueError):
        return default


class Logger:
    def __init__(self, quiet=False, path=LOG):
        self.quiet, self.path = quiet, path

    def __call__(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        if not self.quiet:
            print(line)
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except IOError:
            pass


# ------------------------------------------------------------------ execucao de comandos
class Runner:
    """Tudo que toca o sistema passa por aqui (nos testes e trocado por uma versao simulada)."""

    def run(self, cmd, timeout=120):
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, errors="replace", timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout apos %ss" % timeout
        except OSError as e:
            return 127, "", str(e)

    def pipeline(self, cmds, timeout, capture=False):
        """Encadeia comandos (cmd1 | cmd2 ...). Devolve (codigos de saida, stdout do ultimo, stderr, segundos)."""
        t0 = time.time()
        errs = [tempfile.TemporaryFile() for _ in cmds]
        procs = []
        try:
            prev = None
            for i, c in enumerate(cmds):
                last = i == len(cmds) - 1
                p = subprocess.Popen(c, stdin=prev.stdout if prev else None,
                                     stdout=subprocess.PIPE if (not last or capture) else subprocess.DEVNULL,
                                     stderr=errs[i])
                if prev:
                    prev.stdout.close()
                procs.append(p)
                prev = p
            out = b""
            try:
                if capture:
                    out = procs[-1].communicate(timeout=timeout)[0]
                else:
                    procs[-1].wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                for p in procs:
                    p.kill()
                for p in procs:
                    p.wait()
                return [124] * len(cmds), "", "timeout apos %ss" % timeout, time.time() - t0
            for p in procs[:-1]:
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            rcs = [p.returncode for p in procs]
            err = b""
            for f in errs:
                f.seek(0)
                err += f.read()[-600:]
            return rcs, out.decode("utf-8", "replace"), err.decode("utf-8", "replace").strip()[-400:], time.time() - t0
        except OSError as e:
            for p in procs:
                p.kill()
            return [127] * len(cmds), "", str(e), time.time() - t0
        finally:
            for f in errs:
                f.close()


# ------------------------------------------------------------------ janela
def parse_hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def window_remaining(now_dt, win):
    """Segundos ate o fim da janela se now_dt esta dentro dela; None se fora."""
    start, end = parse_hhmm(win["start"]), parse_hhmm(win["end"])
    days = [DOW[d[:3].lower()] for d in win["days"]]
    minutes = now_dt.hour * 60 + now_dt.minute
    one = datetime.timedelta(days=1)

    def at(d, mm):
        return datetime.datetime.combine(d, datetime.time(mm // 60, mm % 60))

    if start > end:
        if minutes >= start:
            start_day, end_dt = now_dt.date(), at(now_dt.date() + one, end)
        elif minutes < end:
            start_day, end_dt = now_dt.date() - one, at(now_dt.date(), end)
        else:
            return None
    else:
        if start <= minutes < end:
            start_day, end_dt = now_dt.date(), at(now_dt.date(), end)
        else:
            return None
    if start_day.weekday() not in days:
        return None
    return (end_dt - now_dt).total_seconds()


# ------------------------------------------------------------------ tags / quem e testado
def load_status(root, cfg, now):
    """Estado que cada host publica no NAS. Devolve {vmid: guest} ou None se ausente/velho."""
    data = load_json(os.path.join(root, cfg["status_file"]), None)
    if not data or "guests" not in data:
        return None
    if now - data.get("generated", 0) > cfg["status_max_age_hours"] * 3600:
        return None
    return dict((g["vmid"], g) for g in data["guests"])


def decide_skip(vmid, status, cfg):
    """Motivo para NAO testar a VM, ou '' se deve ser testada."""
    rt = (cfg.get("require_tag") or "").strip()
    if status is None:
        return "sem-estado" if rt else ""   # sem ler as tags nao da pra saber quem foi marcado: nao testa nada
    g = status.get(vmid)
    if g is None:
        return "vm-removida"
    tags = g.get("tags") or []
    if g.get("state") == "isento" or "sem-backup" in tags:
        return "isento"
    if g.get("state") == "sem_job":
        return "sem-job"
    if rt and rt not in tags:
        return "nao-marcada"
    return ""


# ------------------------------------------------------------------ inventario (backups no NAS)
def scan_dump_dir(dump_dir):
    """Backup mais recente de cada VMID dentro de uma pasta dump/."""
    best = {}
    for n in os.listdir(dump_dir):
        m = FILE_RE.match(n)
        if not m:
            continue
        p = os.path.join(dump_dir, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        name_ts = "".join(m.group(i) for i in range(3, 9))
        cur = best.get(m.group(2))
        if cur is None or name_ts > cur["name_ts"]:
            best[m.group(2)] = {"vmid": m.group(2), "kind": "qemu" if m.group(1) == "qemu" else "lxc", "fname": n,
                                "path": p, "size": st.st_size, "ts": int(st.st_mtime), "name_ts": name_ts}
    return best


def build_inventory(sources, cfg, now):
    """sources: {host: {"dump": pasta dump/, "root": pasta raiz da fonte}} -> lista de entradas."""
    inv = []
    for host, src in sources.items():
        status = load_status(src["root"], cfg, now)
        for vmid, e in sorted(scan_dump_dir(src["dump"]).items(), key=lambda kv: int(kv[0])):
            g = (status or {}).get(vmid, {})
            e = dict(e, host=host, key="%s/%s" % (host, vmid), name=g.get("name", ""), tags=g.get("tags") or [],
                     skip=decide_skip(vmid, status, cfg), status_known=status is not None)
            inv.append(e)
    return inv


# ------------------------------------------------------------------ planejamento (funcao pura)
def new_state():
    return {"version": VERSION, "vms": {}, "last_run": {"ts": 0, "result": "none", "msg": ""}, "cleanup_pending": 0}


def touch_inventory(state, inventory, now):
    for e in inventory:
        rec = state["vms"].setdefault(e["key"], {})
        rec.setdefault("first_seen", int(now))


def plan_tests(inventory, state, cfg, now, restore_enabled=None):
    """Lista ordenada de {entry, level, reason} devidos agora (uma linha por VM)."""
    enabled = cfg["restore"]["enabled"] if restore_enabled is None else restore_enabled
    plan = []
    for e in inventory:
        if e.get("skip"):
            continue
        if (now - e["ts"]) / 86400.0 > cfg["max_backup_age_days"]:
            continue
        rec = state["vms"].get(e["key"], {})
        integ, rest = rec.get("integrity"), rec.get("restore")
        i_due, reason = False, ""
        if integ is None:
            i_due, reason = True, "nunca testada"
        elif not integ.get("ok"):
            i_due, reason = True, "ultimo teste de integridade falhou"
        else:
            age = (now - integ["ts"]) / 86400.0
            if age >= cfg["integrity_max_days"]:
                i_due, reason = True, "integridade ha %.0f dias" % age
            elif integ.get("file") != e["fname"] and age >= cfg["integrity_every_days"] - TOLERANCE_DAYS:
                i_due, reason = True, "backup novo para verificar"
        r_due = False
        if enabled and e["kind"] == "qemu":
            skip = rec.get("restore_skip")
            if skip == "grande" and (rec.get("prov_gb") or 0) <= cfg["restore_max_gb"]:
                skip = None     # o limite foi aumentado depois que a VM foi dispensada
            if not skip:
                size_gb = max(rec.get("prov_gb") or 0, e["size"] / 2.0 ** 30)
                min_days = cfg["restore_big_min_days"] if size_gb > cfg["restore_big_gb"] else cfg["restore_min_days"]
                if rest is None:
                    r_due, reason = True, "restore nunca testado"
                else:
                    age = (now - rest["ts"]) / 86400.0
                    if rest.get("ok"):
                        if rest.get("file") != e["fname"] and age >= min_days - TOLERANCE_DAYS:
                            r_due, reason = True, "backup novo para restaurar"
                    elif age >= cfg["restore_retry_days"] - TOLERANCE_DAYS:
                        r_due, reason = True, "novo teste de restore apos falha"
        if not (i_due or r_due):
            continue
        ok_ts = max([x["ts"] for x in (integ, rest) if x and x.get("ok")] or [0])
        failed_last = bool(rec.get("last") and not rec["last"].get("ok"))
        klass = 0 if ok_ts == 0 else (1 if failed_last else 2)
        plan.append({"entry": e, "level": "restore" if r_due else "integrity", "reason": reason,
                     "prio": (klass, ok_ts, e["size"]), "prov_gb": rec.get("prov_gb")})
    plan.sort(key=lambda p: p["prio"])
    return plan


def estimate_seconds(item, cfg, prov_gb=None):
    read = cfg["nas_read_mb_s"] * 1024 * 1024.0
    size = item["entry"]["size"]
    secs = size / read
    if item["level"] == "restore":
        # leitura do NAS e escrita no storage andam em paralelo: vale o mais lento, mais uma folga e o boot
        factor = 1.3 if str(item["entry"].get("fname", "")).endswith(".vma") else 3.0
        gb = prov_gb or item.get("prov_gb") or size / 2.0 ** 30 * factor
        secs = max(secs, gb * 2.0 ** 30 / read) * 1.15 + cfg["restore"]["boot_timeout_min"] * 60 * 0.5 + 180
    return secs + 60


def window_seconds(win):
    return ((parse_hhmm(win["end"]) - parse_hhmm(win["start"])) % 1440) * 60


def pack_nights(plan, cfg, night_secs):
    """Simula como o plano se divide em noites (mesma regra do --run: so comeca teste que cabe na janela).
    Devolve (lista de noites, cada uma com os itens do dia; itens que nao cabem nem numa noite inteira)."""
    pending, nights = list(plan), []
    while pending:
        left, restores, today, rest = night_secs, 0, [], []
        for p in pending:
            q = dict(p, level="integrity") if p["level"] == "restore" and restores >= cfg["restore"]["max_per_night"] else p
            est = estimate_seconds(q, cfg)
            if est <= left - 300:
                today.append(q)
                left -= est
                restores += 1 if q["level"] == "restore" else 0
            else:
                rest.append(p)
        if not today:
            return nights, rest
        nights.append(today)
        pending = rest
    return nights, []


# ------------------------------------------------------------------ trava de backup em andamento
# Regra: nao comeca teste enquanto ha backup acontecendo (vzdump neste no, ou arquivo parcial sendo gravado no NAS) nem
# quando um job de backup deste no vai comecar antes do teste terminar. Espera o backup passar e continua na mesma noite.
_AVISADOS = set()


def _json_list(text):
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, list) else None


def backup_running(runner, cfg, node, srcs, now):
    """Texto com o motivo se ha backup em andamento agora; "" se nao ha. Nunca levanta excecao."""
    g = cfg["guard"]
    why = []
    rc, out, _ = runner.run(["pvesh", "get", "/nodes/%s/tasks" % node, "--source", "active", "--output-format", "json"], 60)
    tasks = _json_list(out) if rc == 0 else None
    if tasks is not None:
        vz = [x for x in tasks if isinstance(x, dict) and x.get("type") == "vzdump"]
        if vz:
            ids = [str(x["id"]) for x in vz if x.get("id")]
            why.append("vzdump em execucao neste no (%s)" % (("VM " + ",".join(ids[:4])) if ids else "job com varias VMs"))
    elif runner.run(["pgrep", "-f", "task UPID.*:vzdump:"], 15)[0] == 0:      # plano B se o pvesh nao responder
        why.append("vzdump em execucao neste no")
    limit = now - g["partial_minutes"] * 60
    for host, src in sorted((srcs or {}).items()):
        try:
            names = os.listdir(src["dump"])
        except OSError:
            continue
        for n in names:
            if n.startswith("vzdump-") and n.endswith((".dat", ".tmp", ".partial")):
                try:
                    if os.stat(os.path.join(src["dump"], n)).st_mtime >= limit:
                        why.append("backup sendo gravado no NAS (%s: %s)" % (host, n[:48]))
                        break
                except OSError:
                    pass
    return "; ".join(why)


def jobs_next(runner, node, now, log=None):
    """Proximo inicio de cada job de backup ATIVO deste no: [(epoch, 'job ID (agenda)')] em ordem. A agenda e calculada
    pelo systemd-analyze, que entende a mesma sintaxe do Proxmox (sat 01:00, *-*-01 02:00, mon..fri 21:00 ...)."""
    rc, out, _ = runner.run(["pvesh", "get", "/cluster/backup", "--output-format", "json"], 60)
    jobs = _json_list(out) if rc == 0 else None
    res = []
    for j in jobs or []:
        if not isinstance(j, dict) or str(j.get("enabled", 1)).lower() in ("0", "false", "no"):
            continue
        if j.get("node") and j["node"] != node:
            continue
        sched = j.get("schedule")
        if not sched and j.get("starttime"):                  # formato antigo (dow + starttime)
            sched = ("%s %s" % (j["dow"], j["starttime"])) if j.get("dow") else str(j["starttime"])
        if not sched:
            continue
        rc, out, _ = runner.run(["systemd-analyze", "calendar", "--iterations=1", str(sched)], 30)
        m = re.search(r"Next elapse:\s+(?:[A-Za-z]{3}\s+)?(\d{4})-(\d\d)-(\d\d)\s+(\d\d):(\d\d):(\d\d)", out) if rc == 0 else None
        if not m:
            if log and sched not in _AVISADOS:
                _AVISADOS.add(sched)
                log("guarda: nao consegui interpretar a agenda '%s' do job %s (a trava so enxerga vzdump em andamento nele)" % (
                    sched, j.get("id", "?")))
            continue
        ts = datetime.datetime(*[int(x) for x in m.groups()]).timestamp()
        if ts > now:
            res.append((ts, "job %s (%s)" % (j.get("id", "?"), sched)))
    return sorted(res)


def guard_status(runner, cfg, node, srcs, now, log=None):
    nxt = jobs_next(runner, node, now, log)
    return {"busy": backup_running(runner, cfg, node, srcs, now),
            "next": nxt[0][0] if nxt else None, "next_desc": nxt[0][1] if nxt else ""}


def guard_wait(runner, cfg, log, node, srcs, now_fn, sleep, gstate, force_window):
    """Espera enquanto ha backup em andamento ou um job deste no esta prestes a comecar (ate margin_min antes).
    gstate["hold"] guarda ate quando segurar depois que um job estava para comecar (o job demora uns segundos para aparecer).
    Devolve (continuar, proximo_inicio_de_job, descricao); continuar=False encerra a rodada de hoje."""
    g = cfg["guard"]
    margin, slept, said = g["margin_min"] * 60, 0, False
    while True:
        now = now_fn()
        gs = guard_status(runner, cfg, node, srcs, now, log)
        nxt = gs["next"]
        if gs["busy"]:
            why = gs["busy"]
        elif nxt and nxt - now <= margin:
            why = "%s comeca em %d min" % (gs["next_desc"], max(0, int(nxt - now) // 60))
            gstate["hold"] = max(gstate["hold"], nxt + 300)
        elif now < gstate["hold"]:
            why = "aguardando o job de backup que esta comecando"
        else:
            if said:
                log("guarda: liberado depois de %d min de espera" % (slept // 60))
            return True, nxt, gs["next_desc"]
        if not said:
            log("guarda: %s - testes em pausa" % why)
            said = True
        if not force_window:
            rem = window_remaining(datetime.datetime.fromtimestamp(now), cfg["window"])
            if rem is None or rem < 900:
                log("guarda: a janela esta acabando; o que falta fica para a proxima noite")
                return False, nxt, gs["next_desc"]
        if slept >= g["max_wait_min"] * 60:
            log("guarda: espera passou de %d min, encerrando a rodada" % g["max_wait_min"])
            return False, nxt, gs["next_desc"]
        sleep(g["poll_seconds"])
        slept += g["poll_seconds"]


def guard_report(runner, cfg, node, now):
    """Linhas do --check sobre a trava (para conferir no pve3 que as duas leituras funcionam)."""
    g = cfg["guard"]
    if not g.get("enabled", True):
        return ["  DESLIGADA na configuracao (guard.enabled = false)"]
    out = []
    rc, txt, err = runner.run(["pvesh", "get", "/nodes/%s/tasks" % node, "--source", "active", "--output-format", "json"], 60)
    tasks = _json_list(txt) if rc == 0 else None
    if tasks is None:
        out.append("  tarefas ativas: NAO CONSEGUI LER (%s) - usando o plano B (pgrep)" % ((err or "").strip()[:80] or "codigo %s" % rc))
    else:
        out.append("  tarefas ativas do no: %d, das quais %d de backup (vzdump)" % (
            len(tasks), sum(1 for x in tasks if isinstance(x, dict) and x.get("type") == "vzdump")))
    jobs = jobs_next(runner, node, now, lambda m: out.append("  " + m))
    for ts, desc in jobs:
        out.append("  proximo inicio: %s em %s" % (desc, time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))))
    if not jobs:
        out.append("  nenhum job de backup ativo e agendado lido neste no")
    out.append("  regra: nao comeca teste que acabaria menos de %d min antes de um job; se um backup esta rodando, espera (olha a cada %d s, ate %d min por vez)" % (
        g["margin_min"], g["poll_seconds"], g["max_wait_min"]))
    out.append("  arquivos parciais (.dat/.tmp) no NAS contam como backup em andamento se mexidos ha menos de %d min" % g["partial_minutes"])
    return out


# ------------------------------------------------------------------ teste de integridade
def integrity_cmds(path):
    dec = None
    if path.endswith(".zst"):
        dec = ["zstd", "-q", "-d", "-c", path]
    elif path.endswith(".lzo"):
        dec = ["lzop", "-d", "-c", path]
    elif path.endswith(".gz"):
        dec = ["gzip", "-d", "-c", path]
    base = path
    for ext in (".zst", ".lzo", ".gz"):
        if base.endswith(ext):
            base = base[:-len(ext)]
    if base.endswith(".vma"):
        return [dec, ["vma", "verify", "-"]] if dec else [["vma", "verify", path]]
    if base.endswith(".tar"):
        return [dec, ["tar", "-tf", "-"]] if dec else [["tar", "-tf", path]]
    raise ValueError("formato nao reconhecido: " + path)


def verify_integrity(runner, entry, cfg):
    gb = entry["size"] / 2.0 ** 30
    timeout = int(600 + gb * 40)
    try:
        cmds = integrity_cmds(entry["path"])
    except ValueError as e:
        return {"ok": False, "secs": 0, "msg": str(e), "final": True}
    names = [c[0] for c in cmds]
    rcs, _, err, secs = runner.pipeline([["nice", "-n", "19"] + c for c in cmds], timeout)
    if all(rc == 0 for rc in rcs):
        return {"ok": True, "secs": round(secs, 1), "msg": ""}
    bad = [names[i] for i, rc in enumerate(rcs) if rc != 0]
    why = "tempo esgotado" if 124 in rcs else ("falha em %s" % ",".join(bad))
    return {"ok": False, "secs": round(secs, 1), "msg": ("%s: %s" % (why, err))[:300]}


# ------------------------------------------------------------------ teste de restore
class Unsafe(Exception):
    pass


def check_vmid(vmid, cfg):
    r = cfg["restore"]
    if not (isinstance(vmid, int) and r["vmid_min"] <= vmid <= r["vmid_max"]):
        raise Unsafe("VMID %r fora da faixa de teste %d-%d" % (vmid, r["vmid_min"], r["vmid_max"]))
    if vmid < 9000:
        raise Unsafe("faixa de teste invalida na configuracao (precisa ser >= 9000)")
    return vmid


def parse_qm_list(text):
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            out.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
    return out


def used_vmids(runner):
    """VMIDs ocupados no cluster inteiro (VM e CT, qualquer no) + os do no local."""
    used = set()
    rc, out, _ = runner.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"], 60)
    if rc == 0:
        try:
            used |= set(int(x["vmid"]) for x in json.loads(out))
        except (ValueError, KeyError, TypeError):
            pass
    used |= set(v for v, _ in parse_qm_list(runner.run(["qm", "list"], 60)[1]))
    for l in runner.run(["pct", "list"], 60)[1].splitlines()[1:]:
        if l.split() and l.split()[0].isdigit():
            used.add(int(l.split()[0]))
    return used


def provisioned_gb(runner, entry):
    path = entry["path"]
    cmds = [["vma", "list", path]] if path.endswith(".vma") else integrity_cmds(path)[:-1] + [["vma", "list", "-"]]
    rcs, out, _, _ = runner.pipeline(cmds, 180, capture=True)
    sizes = [int(x) for x in re.findall(r"DEV: dev_id=\d+ size: (\d+)", out)]
    return sum(sizes) / 2.0 ** 30 if sizes else None


def free_pool_gb(runner, storage):
    rc, out, _ = runner.run(["pvesm", "status", "--storage", storage], 60)
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 6 and p[0] == storage:
            return int(p[5]) / 1048576.0
    return None


def cleanup_vm(runner, vmid, cfg, log):
    check_vmid(vmid, cfg)
    runner.run(["qm", "stop", str(vmid), "--skiplock", "1"], 120)
    runner.run(["qm", "set", str(vmid), "--protection", "0"], 60)
    runner.run(["qm", "destroy", str(vmid), "--purge", "1", "--destroy-unreferenced-disks", "1", "--skiplock", "1"], 300)
    gone = runner.run(["qm", "status", str(vmid)], 30)[0] != 0
    if not gone:
        log("ATENCAO: VM temporaria %d nao foi removida" % vmid)
    return gone


def sweep_leftovers(runner, cfg, log):
    """Remove VMs de teste que sobraram de uma execucao interrompida (so faixa de teste e nome rt-*)."""
    r = cfg["restore"]
    left = 0
    for vmid, name in parse_qm_list(runner.run(["qm", "list"], 60)[1]):
        if r["vmid_min"] <= vmid <= r["vmid_max"] and name.startswith("rt-"):
            log("removendo VM de teste que sobrou: %d (%s)" % (vmid, name))
            if not cleanup_vm(runner, vmid, cfg, log):
                left += 1
    return left


# Chaves da VM restaurada que sao removidas antes de ligar: dependem do host de origem ou conflitam com o limite de 2 nucleos / 2 GB.
# (vcpus de hotplug maior que --cores e o caso classico: "Invalid CPU topology ... maxcpus < smp_cpus"; hookscript/args rodam algo no host.)
HOST_KEYS = re.compile(r"^(numa\d+|vcpus|affinity|hugepages|keephugepages|hookscript|args|audio\d+|rng\d+|ivshmem)$")


def restore_test(runner, entry, cfg, log, sleep=time.sleep, clock=time.time):
    """Restaura numa VM temporaria sem rede, sobe, confere o boot e destroi. Devolve o resultado."""
    r = cfg["restore"]
    t0 = clock()
    created = [False]
    vmid_box = [None]
    prov = [None]   # GB provisionados (so conhecidos depois de ler o cabecalho do backup)

    def fail(msg, **extra):
        d = {"ok": False, "secs": round(clock() - t0, 1), "msg": msg, "boot_check": "", "cleanup_ok": True, "prov_gb": prov[0]}
        d.update(extra)
        return d

    def attempt():
        if entry["kind"] != "qemu":
            return fail("restore so e feito para VM (CT fica so na integridade)", skip="ct")
        gb = provisioned_gb(runner, entry)
        if gb is None:
            return fail("nao consegui ler o tamanho dos discos do backup", skip="tamanho desconhecido")
        prov[0] = round(gb, 1)
        if gb > cfg["restore_max_gb"]:
            return fail("discos de %.0f GB acima do limite de %d GB (so integridade)" % (gb, cfg["restore_max_gb"]), skip="grande")
        free = free_pool_gb(runner, r["target_storage"])
        if free is None or free < gb * r["free_factor"]:
            return fail("espaco insuficiente em %s (livre %s GB, preciso %.0f GB)" % (
                r["target_storage"], "?" if free is None else "%.0f" % free, gb * r["free_factor"]))
        used = used_vmids(runner)
        free_ids = [i for i in range(r["vmid_min"], r["vmid_max"] + 1) if i not in used]
        if not free_ids:
            return fail("sem VMID livre na faixa de teste")
        vmid = check_vmid(free_ids[0], cfg)
        vmid_box[0] = vmid
        created[0] = True
        rc, _, err = runner.run(["qmrestore", entry["path"], str(vmid), "--storage", r["target_storage"], "--unique", "1"], int(1800 + gb * 60))
        if rc != 0:
            return fail("qmrestore falhou: %s" % err[-200:])
        # protecao PRIMEIRO: com protection=1 o Proxmox recusa remover qualquer drive (ate o CD vazio) e o "qm set --delete" falha inteiro
        runner.run(["qm", "set", str(vmid), "--protection", "0"], 60)
        conf = runner.run(["qm", "config", str(vmid)], 60)[1]
        dels = []
        for line in conf.splitlines():
            k = line.split(":", 1)[0]
            if re.match(r"^(net|hostpci|usb|serial|parallel)\d+$", k):
                dels.append(k)
            elif re.match(r"^(ide|sata|scsi|virtio)\d+$", k) and "media=cdrom" in line:
                dels.append(k)
            elif HOST_KEYS.match(k):
                dels.append(k)   # CPU hotplug/NUMA/afinidade e itens presos ao host de origem (impedem subir com 2 nucleos / 2 GB)
        del_err = ""
        if dels:
            rc, out, err = runner.run(["qm", "set", str(vmid), "--delete", ",".join(dels)], 60)
            if rc != 0:
                del_err = " | ".join(x.strip()[-150:] for x in (out, err) if x and x.strip())
        runner.run(["qm", "set", str(vmid), "--memory", str(r["memory_mb"]), "--balloon", "0", "--cores", str(r["cores"]),
                    "--sockets", "1", "--numa", "0", "--onboot", "0", "--name", "rt-%s" % entry["vmid"], "--tags", "restore-test;sem-backup"], 60)
        conf = runner.run(["qm", "config", str(vmid)], 60)[1]
        if re.search(r"^net\d+:", conf, re.M):
            return fail("a VM de teste ainda tem placa de rede, abortei sem ligar" + (" (qm set: %s)" % del_err if del_err else ""))
        agent_on = bool(re.search(r"^agent:\s*(1|enabled=1)", conf, re.M))
        rc, out, err = runner.run(["qm", "start", str(vmid)], 180)
        if rc != 0:
            # o motivo do QEMU (linha "kvm: ...") sai no stdout; o stderr so traz "start failed: QEMU exited with code 1"
            return fail("qm start falhou: %s" % " | ".join(x.strip()[-250:] for x in (out, err) if x and x.strip()))
        boot, last_read = "", 0.0
        start = clock()
        deadline = start + r["boot_timeout_min"] * 60
        node = socket.gethostname().split(".")[0]
        while clock() < deadline:
            sleep(15)
            if "running" not in runner.run(["qm", "status", str(vmid)], 30)[1]:
                return fail("a VM desligou sozinha durante o boot")
            if agent_on and runner.run(["qm", "agent", str(vmid), "ping"], 30)[0] == 0:
                boot = "agent"
                break
            cur = runner.run(["pvesh", "get", "/nodes/%s/qemu/%d/status/current" % (node, vmid), "--output-format", "json"], 30)[1]
            try:
                last_read = float(json.loads(cur).get("diskread", 0)) / 1048576.0
            except (ValueError, TypeError, AttributeError):
                pass
            if clock() - start >= r["boot_min_seconds"] and last_read >= r["boot_min_diskread_mb"]:
                boot = "heuristica"
                break
        if not boot:
            return fail("sem sinal de boot em %d min (leitura de disco %.0f MB)" % (r["boot_timeout_min"], last_read))
        return {"ok": True, "secs": round(clock() - t0, 1), "boot_check": boot, "cleanup_ok": True, "prov_gb": prov[0],
                "msg": "boot confirmado por %s" % ("agent" if boot == "agent" else "leitura de disco")}

    res = None
    try:
        res = attempt()
    except Unsafe:
        raise
    except Exception as e:  # nunca deixar VM de teste para tras
        res = fail("erro inesperado: %s" % e)
    finally:
        if created[0]:
            ok = cleanup_vm(runner, vmid_box[0], cfg, log)
            if res is not None:
                res["cleanup_ok"] = ok
                if not ok:
                    res["msg"] += " [VM de teste NAO removida]"
    return res


# ------------------------------------------------------------------ montagem do NAS (somente leitura)
def parse_storage_cfg(text, storage_id):
    cur, out = None, {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s*(\S+)\s*$", line)
        if m:
            cur = m.group(2) if m.group(1) == "cifs" else None
            continue
        if cur == storage_id:
            kv = line.strip().split(None, 1)
            if len(kv) == 2:
                out[kv[0]] = kv[1].strip()
    return out


def mount_sources(runner, cfg, log):
    """Monta cada subpasta do NAS somente leitura. Devolve ({host: {dump, root}}, {host: erro})."""
    with open(PVE_STORAGE_CFG) as f:
        sc = parse_storage_cfg(f.read(), cfg["storage_id"])
    for k in ("server", "share", "username"):
        if k not in sc:
            raise RuntimeError("storage '%s' nao encontrado ou incompleto em %s" % (cfg["storage_id"], PVE_STORAGE_CFG))
    with open(PVE_PRIV % cfg["storage_id"]) as f:
        raw = f.read().strip()
    m = re.search(r"^password=(.*)$", raw, re.M)   # o Proxmox grava 'password=SENHA' neste arquivo
    pw = m.group(1) if m else raw
    ctx = "usuario=%s dominio=%s smb=%s" % (sc["username"], sc.get("domain", "-"), sc.get("smbversion", "auto"))
    fd = os.open(CRED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("username=%s\npassword=%s\n" % (sc["username"], pw))
        if sc.get("domain"):
            f.write("domain=%s\n" % sc["domain"])
    srcs, errs = {}, {}
    try:
        for s in cfg["sources"]:
            mp = os.path.join(MOUNT_BASE, s["subdir"])
            os.makedirs(mp, exist_ok=True)
            if os.path.ismount(mp):
                runner.run(["umount", mp], 60)
            opts = "ro,credentials=%s,soft,nounix,serverino" % CRED
            if sc.get("smbversion") and sc["smbversion"] != "default":
                opts += ",vers=%s" % sc["smbversion"]
            rc, _, err = runner.run(["mount", "-t", "cifs", "//%s/%s/%s" % (sc["server"], sc["share"], s["subdir"]), mp, "-o", opts], 60)
            if rc != 0:
                errs[s["host"]] = "mount falhou (%s): %s" % (ctx, err.strip().splitlines()[0][:120] if err.strip() else rc)
                continue
            dump = os.path.join(mp, "dump")
            if os.path.isdir(dump):
                srcs[s["host"]] = {"dump": dump, "root": mp}
            else:
                errs[s["host"]] = "pasta dump/ nao existe em %s" % s["subdir"]
                runner.run(["umount", mp], 60)
    finally:
        try:
            os.remove(CRED)
        except OSError:
            pass
    return srcs, errs


def unmount_sources(runner, cfg):
    for s in cfg["sources"]:
        mp = os.path.join(MOUNT_BASE, s["subdir"])
        if os.path.ismount(mp):
            if runner.run(["umount", mp], 60)[0] != 0:
                runner.run(["umount", "-l", mp], 60)


# ------------------------------------------------------------------ resultado / relatorio
def record_result(state, entry, level, res, now):
    rec = state["vms"].setdefault(entry["key"], {"first_seen": int(now)})
    item = {"ts": int(now), "ok": bool(res["ok"]), "file": entry["fname"], "size": entry["size"],
            "secs": res.get("secs", 0), "msg": res.get("msg", "")}
    if level == "restore":
        if res.get("prov_gb"):
            rec["prov_gb"] = res["prov_gb"]
        if res.get("skip"):
            rec["restore_skip"] = res["skip"]
            return None
        item["boot"] = res.get("boot_check", "")
        rec["restore"] = item
        if res["ok"]:
            rec["integrity"] = dict(item)
    else:
        rec["integrity"] = item
    rec["last"] = {"ts": item["ts"], "ok": item["ok"], "level": level, "msg": item["msg"]}
    return item


def build_report(inventory, state, cfg, now, node):
    tests, never, failed, overdue, ok_ages = [], 0, 0, 0, []
    for e in inventory:
        rec = state["vms"].get(e["key"], {})
        integ, rest, last = rec.get("integrity"), rec.get("restore"), rec.get("last")
        oks = [x for x in (integ, rest) if x and x.get("ok")]
        ok_ts = max([x["ts"] for x in oks]) if oks else None
        recent = (now - e["ts"]) / 86400.0 <= cfg["max_backup_age_days"]
        eligible = recent and not e.get("skip")
        if eligible:
            if ok_ts is None:
                if not (last and not last["ok"]):
                    never += 1
                if (now - rec.get("first_seen", now)) / 86400.0 > cfg["overdue_days"]:
                    overdue += 1
            else:
                ok_ages.append((now - ok_ts) / 86400.0)
                if (now - ok_ts) / 86400.0 > cfg["overdue_days"]:
                    overdue += 1
            if last and not last["ok"]:
                failed += 1
        tests.append({
            "key": e["key"], "host": e["host"], "vmid": e["vmid"], "name": e.get("name", ""), "kind": e["kind"],
            "eligible": eligible, "skip": e.get("skip") or ("backup-antigo" if not recent else ""),
            "last_backup_ts": e["ts"], "last_backup_file": e["fname"],
            "last_ok_ts": ok_ts, "last_ts": last["ts"] if last else None,
            "last_result": ("ok" if last["ok"] else "failed") if last else "none",
            "last_level": last["level"] if last else None, "last_msg": last["msg"][:160] if last else "",
            "integrity_ts": integ["ts"] if integ else None, "integrity_ok": bool(integ and integ.get("ok")),
            "restore_ts": rest["ts"] if rest else None, "restore_ok": bool(rest and rest.get("ok")),
            "restore_note": rec.get("restore_skip") or (rest.get("boot", "") if rest else ""),
            "prov_gb": rec.get("prov_gb"),
        })
    lr = state.get("last_run", {})
    return {
        "version": VERSION, "generated": int(now), "node": node,
        "summary": {"vms": sum(1 for t in tests if t["eligible"]), "tested_ok": sum(1 for t in tests if t["eligible"] and t["last_ok_ts"]),
                    "failed": failed, "never": never, "overdue": overdue,
                    "oldest_ok_days": round(max(ok_ages), 1) if ok_ages else -1,
                    "skipped": sum(1 for t in tests if t["skip"]),
                    "last_run_ts": lr.get("ts", 0), "last_run_result": lr.get("result", "none"),
                    "cleanup_pending": int(state.get("cleanup_pending", 0)),
                    "restore_enabled": 1 if cfg["restore"]["enabled"] else 0},
        "tests": tests,
    }


def write_report(inventory, state, cfg, now, node, out=OUT):
    atomic_write(out, json.dumps(build_report(inventory, state, cfg, now, node), separators=(",", ":")))


# ------------------------------------------------------------------ execucao
def run_tests(runner, cfg, log, now_fn=time.time, only=None, level=None, force_window=False, sleep=time.sleep,
              mount=mount_sources, unmount=unmount_sources, state_path=STATE, inv_path=INVENTORY, out=OUT, hist=HISTORY,
              use_guard=True):
    node = socket.gethostname().split(".")[0]
    state = load_json(state_path, None) or new_state()
    srcs, errs = mount(runner, cfg, log)
    for h, e in errs.items():
        log("fonte %s indisponivel: %s" % (h, e))
    try:
        inventory = build_inventory(srcs, cfg, now_fn())
        for h in sorted(set(e["host"] for e in inventory if not e["status_known"])):
            log("aviso: sem estado recente de %s no NAS, nao da pra ler as tags (%s)" % (
                h, "nenhuma VM sera testada" if cfg["require_tag"] else "testando tudo que tem backup"))
        atomic_write(inv_path, json.dumps(inventory))
        touch_inventory(state, inventory, now_fn())
        state["cleanup_pending"] = sweep_leftovers(runner, cfg, log)
        done, restores, attempted, summary = 0, 0, set(), "none"
        g_on = use_guard and cfg["guard"].get("enabled", True)
        gstate, holds = {"hold": 0}, 0
        while True:
            now = now_fn()
            remaining = None
            if not force_window:
                remaining = window_remaining(datetime.datetime.fromtimestamp(now), cfg["window"])
                if remaining is None:
                    log("fora da janela de testes, encerrando")
                    break
            nxt, ndesc = None, ""
            if g_on:
                go, nxt, ndesc = guard_wait(runner, cfg, log, node, srcs, now_fn, sleep, gstate, force_window)
                if not go:
                    break
                now = now_fn()
                if not force_window:
                    remaining = window_remaining(datetime.datetime.fromtimestamp(now), cfg["window"])
                    if remaining is None:
                        log("fora da janela de testes, encerrando")
                        break
            if only:
                ent = [e for e in inventory if e["key"] == only]
                if not ent:
                    log("VM %s nao encontrada no inventario" % only)
                    break
                plan = [{"entry": ent[0], "level": level or "integrity", "reason": "pedido manual"}]
            else:
                plan = plan_tests(inventory, state, cfg, now)
            room = remaining
            if nxt:
                lim = nxt - now - cfg["guard"]["margin_min"] * 60      # tempo ate o proximo job de backup deste no
                room = lim if room is None else min(room, lim)
            item, held = None, False
            for p in plan:
                if p["entry"]["key"] in attempted:
                    continue
                if p["level"] == "restore" and not only and restores >= cfg["restore"]["max_per_night"]:
                    p = dict(p, level="integrity")
                if room is not None and estimate_seconds(p, cfg) > room - 300:
                    if remaining is None or estimate_seconds(p, cfg) <= remaining - 300:
                        held = True        # cabe na janela, so nao cabe antes do proximo job de backup
                    continue
                item = p
                break
            if item is None:
                if held and nxt and holds < 20:
                    holds += 1
                    gstate["hold"] = max(gstate["hold"], nxt + 300)
                    log("guarda: os testes que faltam nao cabem antes do %s; esperando o backup passar" % ndesc)
                    continue
                break
            e = item["entry"]
            attempted.add(e["key"])
            log("testando %s (%s): %s" % (e["key"], item["level"], item["reason"]))
            if item["level"] == "integrity":
                res = verify_integrity(runner, e, cfg)
                left = None if remaining is None else remaining - (now_fn() - now)
                if not res["ok"] and not res.get("final") and (left is None or estimate_seconds(item, cfg) * 1.5 < left - 300):
                    log("  -> falhou (%s); confirmando com uma segunda leitura em 60 s" % res["msg"][:120])
                    sleep(60)
                    again = verify_integrity(runner, e, cfg)
                    res = dict(again, msg="ok na 2a leitura (1a: %s)" % res["msg"][:100]) if again["ok"] else again
            else:
                res = restore_test(runner, e, cfg, log, sleep=sleep)
                restores += 1
                if not res.get("cleanup_ok", True):
                    state["cleanup_pending"] = state.get("cleanup_pending", 0) + 1
            rec = record_result(state, e, item["level"], res, now_fn())
            if rec is not None:
                with open(hist, "a") as f:
                    f.write(json.dumps({"key": e["key"], "level": item["level"], "ok": res["ok"], "secs": res.get("secs"),
                                        "msg": res.get("msg"), "file": e["fname"], "ts": int(now_fn())}) + "\n")
            done += 1
            if not res["ok"]:
                summary = "failed"
            elif summary == "none":
                summary = "ok"
            log("  -> %s em %ss %s" % ("OK" if res["ok"] else "FALHOU", res.get("secs"), res.get("msg", "")))
            atomic_write(state_path, json.dumps(state))
            write_report(inventory, state, cfg, now_fn(), node, out)
        state["last_run"] = {"ts": int(now_fn()), "result": summary, "msg": "%d teste(s)" % done}
        atomic_write(state_path, json.dumps(state))
        write_report(inventory, state, cfg, now_fn(), node, out)
        return done
    finally:
        unmount(runner, cfg)


def refresh(cfg, now=None, state_path=STATE, inv_path=INVENTORY, out=OUT):
    now = now or time.time()
    state = load_json(state_path, None) or new_state()
    inventory = load_json(inv_path, [])
    write_report(inventory, state, cfg, now, socket.gethostname().split(".")[0], out)


def fmt_ts(ts):
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else "-"


def print_status(inventory, state):
    print("%-16s %-12s %-5s %-13s %-17s %-17s %s" % ("VM (host/vmid)", "nome", "tipo", "backup", "integridade", "restore", "situacao"))
    for e in inventory:
        rec = state["vms"].get(e["key"], {})
        i, r, last = rec.get("integrity"), rec.get("restore"), rec.get("last")
        sit = ("nao testa: %s" % e["skip"]) if e.get("skip") else ((last["msg"] or ("ok" if last["ok"] else "falhou")) if last else "-")
        print("%-16s %-12s %-5s %-13s %-17s %-17s %s" % (
            e["key"], (e.get("name") or "")[:12], e["kind"], fmt_ts(e["ts"]),
            ("%s %s" % (fmt_ts(i["ts"]), "ok" if i["ok"] else "FALHOU")) if i else "nunca",
            ("%s %s" % (fmt_ts(r["ts"]), "ok" if r["ok"] else "FALHOU")) if r else (rec.get("restore_skip") or "nunca"), sit))


def cmd_check(runner, cfg, log):
    import shutil
    print("== ferramentas")
    for t in ("zstd", "lzop", "gzip", "tar", "vma", "qm", "qmrestore", "pvesm", "mount.cifs"):
        print("  %-10s %s" % (t, shutil.which(t) or "NAO ENCONTRADO"))
    print("== janela: %s %s-%s | restore %s | tag obrigatoria: %s" % (
        "/".join(cfg["window"]["days"]), cfg["window"]["start"], cfg["window"]["end"],
        "LIGADO" if cfg["restore"]["enabled"] else "desligado", ("so VMs com a tag '%s'" % cfg["require_tag"]) if cfg["require_tag"] else "nenhuma (testa tudo que tem backup)"))
    rem = window_remaining(datetime.datetime.now(), cfg["window"])
    print("  agora: %s" % ("dentro da janela (%.1f h restantes)" % (rem / 3600.0) if rem is not None else "fora da janela"))
    print("== NAS (somente leitura)")
    try:
        srcs, errs = mount_sources(runner, cfg, log)
    except Exception as e:
        print("  ERRO: %s" % e)
        return 1
    try:
        now = time.time()
        for s in cfg["sources"]:
            h = s["host"]
            if h in srcs:
                inv = scan_dump_dir(srcs[h]["dump"])
                status = load_status(srcs[h]["root"], cfg, now)
                big = max(inv.values(), key=lambda x: x["size"]) if inv else None
                tested = sum(1 for v in inv if decide_skip(v, status, cfg) == "")
                print("  %-12s ok: %d VMs com backup, %d a testar%s | estado/tags: %s" % (
                    h, len(inv), tested, (" (maior: VM %s, %.0f GB)" % (big["vmid"], big["size"] / 2.0 ** 30)) if big else "",
                    "lido" if status is not None else "NAO ENCONTRADO/VELHO (ver cron de copia do estado)"))
                if cfg["require_tag"] and status is not None and tested == 0:
                    print("               nenhuma VM com backup tem a tag '%s' ainda (marque as VMs a testar no Proxmox)" % cfg["require_tag"])
            else:
                print("  %-12s PROBLEMA: %s" % (h, errs.get(h)))
    finally:
        unmount_sources(runner, cfg)
    print("== trava de backup (nao testa durante backup nem pouco antes de um job)")
    for line in guard_report(runner, cfg, socket.gethostname().split(".")[0], time.time()):
        print(line)
    free = free_pool_gb(runner, cfg["restore"]["target_storage"])
    print("== espaco livre em %s (destino do restore de teste): %s" % (
        cfg["restore"]["target_storage"], ("%.0f GB" % free) if free is not None else "NAO ENCONTRADO - ajuste restore.target_storage"))
    print("== storages que aceitam discos de VM neste host")
    for line in runner.run(["pvesm", "status", "--content", "images"], 60)[1].splitlines():
        print("  " + line)
    return 0


CRON_TEXT = ("# gerado por pve-restore-tester.py --install\n"
             "PATH=/usr/sbin:/usr/bin:/sbin:/bin\n"
             "0 23 * * 0-4 root flock -n /run/pve-restore-tester.run.lock /usr/bin/python3 %(s)s --run --quiet\n"
             "*/5 * * * * root /usr/bin/python3 %(s)s --refresh --quiet\n")


def main():
    ap = argparse.ArgumentParser()
    for f in ("--check", "--plan", "--status", "--run", "--refresh", "--install", "--quiet", "--force-window", "--sem-guarda"):
        ap.add_argument(f, action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--level", choices=["integrity", "restore"])
    ap.add_argument("--conf", default=CONF)
    a = ap.parse_args()
    cfg = load_conf(a.conf)
    log = Logger(a.quiet)
    runner = Runner()

    if a.install:
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        if not os.path.exists(a.conf):
            atomic_write(a.conf, json.dumps(DEFAULTS, indent=2) + "\n")
            print("configuracao criada em %s (restore desligado)" % a.conf)
        script = os.path.abspath(__file__)
        os.chmod(script, 0o755)
        atomic_write(CRON, CRON_TEXT % {"s": script})
        refresh(cfg)
        print("agendado em %s: testes seg-qui a partir das 23h; arquivo de resultado atualizado a cada 5 min" % CRON)
        return 0
    if a.check:
        return cmd_check(runner, cfg, log)
    if a.refresh:
        refresh(cfg)
        return 0
    if a.status:
        print_status(load_json(INVENTORY, []), load_json(STATE, None) or new_state())
        return 0

    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("ja existe uma execucao em andamento")
        return 1
    if a.plan:
        srcs, errs = mount_sources(runner, cfg, log)
        try:
            now = time.time()
            inv = build_inventory(srcs, cfg, now)
            state = load_json(STATE, None) or new_state()
            plan = plan_tests(inv, state, cfg, now)
            rem = window_remaining(datetime.datetime.now(), cfg["window"])
            skipped = [e for e in inv if e.get("skip")]
            print("fontes indisponiveis: %s" % (errs or "nenhuma"))
            print("%d VM(s) com backup; %d fora do teste; %d teste(s) devidos; janela: %s" % (
                len(inv), len(skipped), len(plan), "aberta" if rem is not None else "fechada agora"))
            for p in plan:
                e = p["entry"]
                print("  %-16s %-9s %7.1f GB  %s  (~%.0f min)" % (e["key"], p["level"], e["size"] / 2.0 ** 30, p["reason"], estimate_seconds(p, cfg) / 60))
            for e in skipped:
                print("  (fora do teste) %-16s %s" % (e["key"], e["skip"]))
            if plan:
                night = window_seconds(cfg["window"])
                nights, stuck = pack_nights(plan, cfg, night)
                print("== simulacao: janela de %.1f h por noite (%s), so comeca teste que cabe no tempo que resta" % (
                    night / 3600.0, "/".join(cfg["window"]["days"])))
                for i, today in enumerate(nights, 1):
                    print("  noite %d: %2d teste(s), ~%.1f h  (%s)" % (
                        i, len(today), sum(estimate_seconds(p, cfg) for p in today) / 3600.0,
                        ", ".join(p["entry"]["key"].split("/")[-1] for p in today)))
                for p in stuck:
                    print("  NAO CABE NUMA NOITE: %s (~%.1f h)" % (p["entry"]["key"], estimate_seconds(p, cfg) / 3600.0))
                print("  total estimado: ~%.1f h em %d noite(s)" % (
                    sum(estimate_seconds(p, cfg) for t in nights for p in t) / 3600.0, len(nights)))
        finally:
            unmount_sources(runner, cfg)
        return 0
    if a.run:
        if a.only and not a.level:
            print("use --level integrity|restore com --only")
            return 2
        n = run_tests(runner, cfg, log, only=a.only, level=a.level, force_window=a.force_window, use_guard=not a.sem_guarda)
        log("fim: %d teste(s) executado(s)" % n)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("ERRO: %s" % e, file=sys.stderr)
        sys.exit(1)
