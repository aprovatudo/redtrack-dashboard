import subprocess
import sys
import os
import re
import streamlit as st

st.set_page_config(
    page_title="Agente Dev",
    page_icon="⚡",
    layout="wide",
)

st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #0f1117; }
[data-testid="stSidebar"] { background: #1a1d27; border-right: 1px solid #2d3148; }
h1 { color: #ffffff !important; font-size: 1.6rem !important; font-weight: 700 !important; }
label { color: #a0a4c0 !important; font-size: 0.85rem !important; }

.stTextInput input {
    background: #1e2130 !important;
    border: 1px solid #2d3148 !important;
    color: #e8eaf0 !important;
    border-radius: 6px !important;
}
div.stButton > button {
    background: #5b6ef5; color: white; border: none;
    border-radius: 8px; padding: 0.55rem 2rem;
    font-weight: 600; font-size: 0.9rem; width: 100%;
}
div.stButton > button:hover { background: #4a5ce0; }

.log-box {
    background: #0d0f18; border: 1px solid #2d3148;
    border-radius: 8px; padding: 1rem 1.2rem;
    font-family: monospace; font-size: 0.78rem; line-height: 1.7;
    max-height: 460px; overflow-y: auto;
    color: #c8cce0; white-space: pre-wrap;
}
.result-card {
    background: #1a1d27; border: 1px solid #2d3148;
    border-radius: 10px; padding: 1.2rem 1.5rem; margin-top: 0.5rem;
}
.result-card table { width: 100%; border-collapse: collapse; }
.result-card td { padding: 0.35rem 0.5rem; color: #c8cce0; font-size: 0.85rem; }
.result-card td:first-child { color: #818cf8; font-weight: 600; width: 140px; }
.badge-ok  { background:#14532d; color:#4ade80; padding:2px 10px; border-radius:20px; font-size:0.75rem; }
.badge-err { background:#450a0a; color:#f87171; padding:2px 10px; border-radius:20px; font-size:0.75rem; }
[data-testid="stCheckbox"] label { color: #94a3b8 !important; font-size: 0.82rem !important; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ Agente Dev")
    st.markdown("<hr style='border-color:#2d3148;margin:0.5rem 0 1rem'>", unsafe_allow_html=True)
    page = st.radio("Módulos", ["Setup BrainMary"], label_visibility="collapsed")
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<span style='color:#3d4166;font-size:0.75rem'>v1.0 · FEG Digital</span>", unsafe_allow_html=True)

# ── Setup BrainMary ──────────────────────────────────────────────────────────
if page == "Setup BrainMary":
    st.markdown("# Setup BrainMary")
    st.markdown("<p style='color:#5b6ef5;font-size:0.85rem;margin-top:-0.5rem'>RedTrack · Vturb · HTML · FTP</p>", unsafe_allow_html=True)
    st.markdown("<hr style='border-color:#2d3148;margin:0.8rem 0 1.5rem'>", unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        lc      = st.text_input("LC", placeholder="LC160")
    with col2:
        conta   = st.text_input("Conta Google Ads", placeholder="123-456-7890")
    with col3:
        dominio = st.text_input("Domínio", placeholder="mynewdomain.online")

    st.markdown("<br>", unsafe_allow_html=True)
    opt1, opt2, opt3, opt4 = st.columns(4)
    with opt1: dry_run       = st.checkbox("Dry-run (simular)")
    with opt2: skip_redtrack = st.checkbox("Pular RedTrack")
    with opt3: skip_vturb    = st.checkbox("Pular Vturb")
    with opt4: skip_ftp      = st.checkbox("Pular FTP")

    st.markdown("<br>", unsafe_allow_html=True)
    run = st.button("▶  Executar Setup", use_container_width=True)

    if run:
        if not all([lc.strip(), conta.strip(), dominio.strip()]):
            st.error("Preencha LC, Conta e Domínio antes de executar.")
            st.stop()

        lc      = lc.strip().upper()
        conta   = conta.strip()
        dominio = dominio.strip().lower()

        script_dir  = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(script_dir, "brainmary_setup.py")
        venv_python = os.path.join(script_dir, "venv", "bin", "python3")
        python_bin  = venv_python if os.path.exists(venv_python) else sys.executable

        cmd = [python_bin, script_path,
               f"--lc={lc}", f"--conta={conta}", f"--dominio={dominio}"]
        if dry_run:       cmd.append("--dry-run")
        if skip_redtrack: cmd.append("--skip-redtrack")
        if skip_vturb:    cmd.append("--skip-vturb")
        if skip_ftp:      cmd.append("--skip-ftp")

        def color_line(line):
            l = line.rstrip()
            if not l:
                return ""
            if l.startswith("=="):
                return f"<span style='color:#818cf8;font-weight:700'>{l}</span>"
            if any(x in l for x in ["✓", " OK", "Pronto", "CONCLUÍDO", "Upload concluído"]):
                return f"<span style='color:#4ade80'>{l}</span>"
            if any(x in l for x in ["ERRO", "Error", "Traceback", "falhou"]):
                return f"<span style='color:#f87171'>{l}</span>"
            if l.startswith("["):
                return f"<span style='color:#93c5fd'>{l}</span>"
            return f"<span style='color:#94a3b8'>{l}</span>"

        st.markdown("**Log de execução**")
        log_area  = st.empty()
        raw_lines = []
        html_lines = []

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=script_dir,
        )

        for line in proc.stdout:
            raw_lines.append(line.rstrip())
            html_lines.append(color_line(line))
            log_area.markdown(
                '<div class="log-box">' + "<br>".join(html_lines) + "</div>",
                unsafe_allow_html=True,
            )

        proc.wait()
        success = proc.returncode == 0

        # ── Resultado ────────────────────────────────────────────────────────
        st.markdown("<br>", unsafe_allow_html=True)
        full_log = "\n".join(raw_lines)
        badge = '<span class="badge-ok">✓ Concluído</span>' if success else '<span class="badge-err">✗ Erro</span>'
        st.markdown(f"**Resultado** &nbsp; {badge}", unsafe_allow_html=True)

        rows = [("LC", lc), ("Conta", conta), ("Domínio", f"fg.{dominio}")]

        m = re.search(r"Campaign ID\s*:\s*(\S+)", full_log)
        if m: rows.append(("Campaign ID", m.group(1)))
        m = re.search(r"Vturb vsl01\s*:\s*(\S+)", full_log)
        if m: rows.append(("Vturb vsl01", m.group(1)))
        m = re.search(r"Vturb micro\s*:\s*(\S+)", full_log)
        if m: rows.append(("Vturb micro", m.group(1)))
        m = re.search(r"Páginas\s*:\s*(.+)", full_log)
        if m: rows.append(("Páginas", m.group(1).strip()))

        table_rows = "".join(
            f"<tr><td>{k}</td><td><code style='color:#e2e8f0;background:#0d0f18;"
            f"padding:1px 6px;border-radius:4px'>{v}</code></td></tr>"
            for k, v in rows
        )
        st.markdown(
            f'<div class="result-card"><table>{table_rows}</table></div>',
            unsafe_allow_html=True,
        )
