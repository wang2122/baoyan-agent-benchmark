"""One fail-closed execution path for local batches and portable answer packets."""
import json
import hashlib
import os
import signal
import ssl
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

from . import cli_adapters, native_auth
from .cli_common import require, write
from .isolation import CandidateSandbox, regular_bytes

PROVIDER_HOSTS = {
    'antigravity': ('daily-cloudcode-pa.googleapis.com', 'cloudcode-pa.googleapis.com',
                    'oauth2.googleapis.com', 'accounts.google.com', 'www.googleapis.com',
                    'lh3.googleusercontent.com'),
    'codex': ('chatgpt.com', 'auth.openai.com', 'api.openai.com'),
    'claude': ('api.anthropic.com', 'claude.ai', 'platform.claude.com'),
    'kimi': ('api.kimi.com', 'api.kimi.ai', 'auth.kimi.com', 'auth.kimi.ai'),
    'gemini': ('cloudcode-pa.googleapis.com', 'oauth2.googleapis.com', 'accounts.google.com',
               'generativelanguage.googleapis.com'),
}
WEB_MODES = {'historical_source_rules_in_prompt', 'live_web', 'provided_materials_only', 'public_web'}


def stage_tls(box, cli):
    names = ('CODEX_CA_CERTIFICATE', 'SSL_CERT_FILE') if cli == 'codex' else ('NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE')
    source = next((os.environ[n] for n in names if os.environ.get(n)), '/private/etc/ssl/cert.pem')
    data = regular_bytes(source, 4 * 1024 * 1024)
    require(b'PRIVATE KEY' not in data and b'BEGIN CERTIFICATE' in data, 'TLS bundle must contain public certificates only')
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=data.decode('ascii'))
    target = box.home / 'ca-bundle.pem'
    target.write_bytes(data)
    box.report['tls_ca_sha256'] = hashlib.sha256(data).hexdigest()
    return {'SSL_CERT_FILE': str(target),
            'CODEX_CA_CERTIFICATE' if cli == 'codex' else 'NODE_EXTRA_CA_CERTS': str(target)}


def network_policy(agent, web_mode):
    require(web_mode in WEB_MODES, 'Unknown task web mode; refuse to guess network permissions')
    hosts = list(PROVIDER_HOSTS.get(agent['cli'], ())) if native_auth.auth_mode(agent) == 'subscription' else [
        urlsplit(agent['base_url']).hostname]
    return hosts, web_mode != 'provided_materials_only'


def index_events(path, target, cli):
    """Lossless normalized envelopes; event index ties each record to native JSONL."""
    count = 0
    with target.open('w', encoding='utf-8') as out:
        if path.is_file():
            for index, line in enumerate(path.read_text(encoding='utf-8', errors='replace').split('\n'), 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    out.write(json.dumps({'line': index, 'cli': cli, 'kind': 'invalid_json'}) + '\n')
                    continue
                if not isinstance(event, dict):
                    continue
                kind = (event.get('event') if cli == 'antigravity' else None) or event.get('type') or event.get('role') or 'provider_event'
                out.write(json.dumps({'line': index, 'cli': cli, 'kind': kind, 'event': event},
                                     ensure_ascii=False) + '\n')
                count += 1
    return count


def parse_capture(cli, events, answer, evidence, *, previous_antigravity=None):
    """Validate the two captured outputs without mistaking a closing reply for the deliverable."""
    answer = Path(answer)
    capture_info = evidence.get('answer_capture')
    if capture_info is None:
        # Old records keep their original final-response contract; never silently repair them.
        return cli_adapters.parse_result(cli, events, answer, previous_antigravity=previous_antigravity)
    final_response = answer.with_name('final-response.md')
    for name, path in (('answer', answer), ('final_response', final_response)):
        data = regular_bytes(path)
        require(hashlib.sha256(data).hexdigest() == capture_info.get(name + '_sha256'),
                'Captured ' + name + ' changed')
    require(capture_info.get('source') in ('workspace_file', 'final_response'), 'Unknown answer source')
    parsed = cli_adapters.parse_result(cli, events, final_response, previous_antigravity=previous_antigravity)
    require(parsed['answer'] == final_response.read_text(encoding='utf-8'), 'Final response/event mismatch')
    if capture_info['source'] == 'final_response':
        require(answer.read_bytes() == final_response.read_bytes(), 'Final-response answer changed')
    parsed['answer'] = answer.read_text(encoding='utf-8')
    require(parsed['answer'].strip(), 'Empty captured answer')
    return parsed


def capture(agent, binary, prompt, web_mode, timeout, events, stderr, answer, evidence, trace,
            *, previous_checkpoint=None, checkpoint=None, require_artifact=True, require_answer_file=True):
    paths = [Path(p) for p in (events, stderr, answer, evidence, trace)]
    events, stderr, answer, evidence, trace = paths
    final_response = answer.with_name('final-response.md')
    paths.append(final_response)
    require(not any(p.exists() or p.is_symlink() for p in paths), 'Capture files already exist')
    require(previous_checkpoint is None or checkpoint is not None, 'Continuation requires an output checkpoint')
    if checkpoint is not None:
        from .native_sessions import capability
        capability(agent['cli'])
        require(not Path(checkpoint).exists() and not Path(checkpoint).is_symlink(), 'Continuation checkpoint already exists')
    meta = {'status': 'running', 'exit_code': None, 'session_id': None, 'reported_models': [],
            'usage': None, 'model_identity_status': 'not_reported', 'error': None,
            'auth_mode': native_auth.auth_mode(agent), 'isolation_verified': False,
            'host_configuration_verified': False, 'execution_evidence': evidence.name}
    box = None
    answer_source = None
    phase = 'environment_setup'
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
    try:
        hosts, public = network_policy(agent, web_mode)
        # Only explicitly chosen credentials and adapter-generated settings survive.
        env_names = (*native_auth.credential_names(agent), 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL',
                     'GOOGLE_GEMINI_BASE_URL', 'AGY_CLI_DISABLE_AUTO_UPDATE', 'NO_COLOR', 'DSH_HOME', 'KIMI_DISABLE_CRON')
        from . import native_sessions
        previous = native_sessions.load(previous_checkpoint) if previous_checkpoint is not None else None
        require(previous is None or previous['cli'] == agent['cli'], 'Continuation CLI changed')
        with CandidateSandbox(binary, hosts, public_https=public, environment_keys=env_names,
                              system_user_lookup=agent['cli'] == 'codex',
                              file_watch=agent['cli'] == 'kimi',
                              loopback_listener=agent['cli'] == 'antigravity',
                              system_trust=agent['cli'] == 'antigravity', allow_pty=agent['cli'] == 'antigravity',
                              resume_directory=previous['directory'] if previous else None) as box:
            (box.work / 'task.json').write_text(json.dumps({'web_mode': web_mode}))
            (box.work / 'query.md').write_text(prompt, encoding='utf-8')
            phase = 'boundary_probes'
            box.verify()
            meta['isolation_verified'] = True
            phase = 'authentication'
            selected = native_auth.stage_auth(box, agent)
            selected.update(stage_tls(box, agent['cli']))
            output = box.work / 'answer.md'
            # Codex writes its last chat message here after tools finish. It must never
            # share the candidate's answer path, including when resuming an episode.
            final_output = box.scratch / 'final-response.md'
            session_id = None
            if checkpoint is not None:
                if previous_checkpoint is not None:
                    session_id = native_sessions.restore(box, previous_checkpoint, agent['cli'])
                (box.work / 'artifacts').mkdir(exist_ok=True)
                args, stdin, env = native_sessions.invocation(agent, binary, prompt, box, final_output, selected, session_id)
            else:
                args, stdin, env = cli_adapters.invocation(agent, binary, prompt, box.work, box.work,
                                                           final_output, box=box, selected_env=selected)
            phase = 'client_execution'
            try:
                result = box.run(args, input=stdin, env=env, timeout=timeout, log_paths=(events, stderr))
            finally:
                # Preserve any written deliverable before sandbox cleanup, also on
                # timeout/interruption. Its presence alone never means success.
                if output.exists() or output.is_symlink():
                    answer.write_bytes(regular_bytes(output))
                    answer_source = 'workspace_file'
                if agent['cli'] == 'codex' and (final_output.exists() or final_output.is_symlink()):
                    final_response.write_bytes(regular_bytes(final_output))
            meta.update(exit_code=result.returncode, status='completed' if result.returncode == 0 else 'failed')
            if meta['status'] == 'completed':
                phase = 'output_validation'
                parsed = cli_adapters.parse_result(agent['cli'], events, final_response,
                    previous_antigravity=previous.get('antigravity_state') if previous else None)
                parsed_answer = parsed.pop('answer')
                if agent['cli'] != 'codex':
                    final_response.write_text(parsed_answer, encoding='utf-8')
                require(answer.exists() or not require_answer_file, 'Missing required answer.md deliverable')
                if not answer.exists():
                    # Multi-turn prompts may request an in-chat answer only. Never
                    # use this fallback for a missing mandatory single-task file.
                    answer.write_bytes(regular_bytes(final_response))
                    answer_source = 'final_response'
                require(regular_bytes(answer).decode('utf-8').strip(), 'Empty answer.md deliverable')
                meta.update(parsed)
                if parsed['reported_models']:
                    observed = parsed.get('primary_reported_models', parsed['reported_models'])
                    match = observed == [agent['model']]
                    meta['model_identity_status'] = 'cli_reported_match' if match else 'cli_reported_mismatch'
                    if not match:
                        meta.update(status='model_mismatch', error='CLI reported other models; review native routing')
                if checkpoint is not None and meta['status'] == 'completed':
                    require(session_id is None or parsed['session_id'] == session_id, 'Native continuation changed session')
                    if require_artifact:
                        require(regular_bytes(box.work / 'artifacts/action-record.md').decode('utf-8').strip(),
                                'Missing mandatory action record')
                    meta['continuation'] = native_sessions.save(box, agent['cli'], parsed['session_id'], checkpoint,
                        antigravity_state=parsed.get('antigravity_state'))
    except subprocess.TimeoutExpired:
        meta.update(status='timeout', error='CLI exceeded the wall-clock limit')
    except KeyboardInterrupt:
        meta.update(status='interrupted', error='User interrupted the run')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        meta.update(status='invalid_output' if meta['status'] == 'completed' else 'failed',
                    error=phase + ' failed (' + type(error).__name__ + '); see setup/check and retained logs')
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        report = dict(box.report) if box is not None else {'status': 'not_started', 'cleanup': 'not_needed'}
        meta['isolation'] = report
        meta['isolation_verified'] = report.get('status') == 'passed' and report.get('cleanup') == 'complete'
        meta['answer_capture'] = {'source': answer_source,
            'answer_sha256': hashlib.sha256(regular_bytes(answer)).hexdigest() if answer.is_file() else None,
            'final_response_sha256': hashlib.sha256(regular_bytes(final_response)).hexdigest() if final_response.is_file() else None}
        count = index_events(events, trace, agent['cli'])
        write(evidence, {'schema': 'native-execution.1', 'auth_mode': native_auth.auth_mode(agent),
            'auth_provenance': 'selected_local_credential_not_account_or_entitlement_verification',
            'cli': agent['cli'], 'cli_version': agent['cli_version'], 'requested_model': agent['model'],
            'web_mode': web_mode, 'source_rules_enforced': False,
            'tools': 'native_tools_inside_outer_sandbox',
            'known_tool_limitations': [],
            'personal_state': 'episode_session_and_artifacts_only' if checkpoint is not None else 'fresh_per_attempt',
            'continuation_input': hashlib.sha256(regular_bytes(previous_checkpoint)).hexdigest()
                                  if previous_checkpoint is not None else None,
            'extra_skills': [], 'extra_mcp_servers': [], 'isolation': report,
            'phase': phase, 'status': meta['status'], 'native_event_count': count,
            'answer_capture': meta['answer_capture'],
            'credentials_included': False, 'official': False})
    return meta
