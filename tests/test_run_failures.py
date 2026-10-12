"""Trust boundaries and actual process exit codes, without provider requests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from benchmark.adapters import ROOT, select_baselines
from benchmark.batch import batch_run
from benchmark.core import load_tasks, read, write
from benchmark.runner import prepare, execute, trusted_launcher


class RunFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.row = load_tasks(ROOT, 'dev')[0]
        self.protocol = read(ROOT / 'config/protocol.json')

    def test_custom_command_cannot_assert_trusted_isolation(self):
        run = prepare(*self.row, self.base / 'fake', self.protocol)
        source = "import os,json;from pathlib import Path;Path(os.environ['BENCH_ANSWER']).write_text('fixture');Path('trajectory.jsonl').write_text('{\"type\":\"fixture\"}\\n');Path('isolation.json').write_text(json.dumps({'backend':'macos-seatbelt-provider-proxy-v1','status':'passed','cleanup':'complete','scope':'local_boundary_probes'}))"
        meta = execute(run, [sys.executable, '-c', source], 'fake', 'v1', 'none', 1, 10)
        self.assertEqual(meta['status'], 'completed')
        self.assertFalse(meta['isolation_verified'])
        self.assertEqual(meta['isolation_evidence_source'], 'untrusted_adapter_report')
        self.assertFalse(trusted_launcher([sys.executable, '-c', source], 'fake', 'v1', 'none'))

    def test_failed_batch_cli_exits_nonzero_and_preserves_state(self):
        b = dict(id='failure-fixture',kind='agent',version='v1',model='none',command=[sys.executable,'-c','raise SystemExit(7)'])
        registry = dict(baselines=[b], repeats=1, timeout_seconds=10)
        path=self.base/'registry.json';write(path,registry)
        out=self.base/'batch'
        p=subprocess.run([sys.executable,'-m','benchmark','batch','--split','dev','--registry',str(path),'--task',self.row[0]['id'],'--out',str(out),'--execute'],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(p.returncode,1,p.stderr)
        result=json.loads(p.stdout)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['statuses'],{'failed':1})
        self.assertEqual(read(out/'failure-fixture'/self.row[0]['id']/'r1/run.json')['exit_code'],7)

    def test_manual_pending_is_normal_but_interruption_has_130(self):
        registry=select_baselines(read(ROOT/'config/baselines.example.json'),['kimi-k3'])
        task,rubric=self.row
        rows=[(dict(task,execution=dict(task['execution'],web_mode='provided_materials_only')),rubric)]
        result=batch_run(rows,self.protocol,registry,self.base/'manual',True)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(result['status'],'waiting_for_app')
        registry=dict(baselines=[dict(id='fake',kind='agent',version='v1',model='none',command=['unused'])],repeats=1,timeout_seconds=10)
        def interrupted(run,*args):
            m=read(run/'run.json');m['status']='interrupted';write(run/'run.json',m);return m
        with patch('benchmark.batch.execute',side_effect=interrupted):
            result=batch_run(rows,self.protocol,registry,self.base/'interrupted',True)
        self.assertEqual(result['exit_code'],130)
        self.assertEqual(result['statuses'],{'interrupted':1})

if __name__=='__main__':unittest.main()
