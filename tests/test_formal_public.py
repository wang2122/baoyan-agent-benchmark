"""Formal registration, immutable capture reuse and result admission boundaries."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from benchmark import formal_public as formal
from benchmark.core import digest, read, write
from benchmark.cli_common import result_exit_code
from benchmark.leaderboard import export_leaderboard

ROOT = Path(__file__).resolve().parents[1]


class FormalPublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        # Preserve the legacy 79-original/4-episode contract using synthetic
        # padding only inside this fixture; current public data stays unchanged.
        self.root = self.base / 'source'
        formal.snapshot(ROOT, self.root)
        manifest_path = self.root / 'data/dev/manifest.json'
        manifest = read(manifest_path)
        self.assertEqual(len(manifest['tasks']), 57)
        staged_count = len(read(self.root / 'data/staged-dev/manifest.json')['tasks'])
        for i in range(79 - staged_count - len(manifest['tasks'])):
            ident = f'LEGACY-SYNTHETIC-{i + 1}'
            demo = self.root / 'examples/public-smoke/REPRO-DEMO-001'
            task, rubric = read(demo / 'task.json'), read(demo / 'rubric.json')
            task.update(id=ident, case_group_id=ident + '-case')
            rubric['id'] = ident
            for node in rubric['nodes']:
                node['id'] = node['id'].replace('REPRO-DEMO-001', ident)
            task['rubric_sha256'] = digest(rubric)
            write(self.root / 'data/dev' / ident / 'task.json', task)
            write(self.root / 'data/dev' / ident / 'rubric.json', rubric)
            manifest['tasks'].append(ident)
            manifest['fingerprints'][ident] = {'task': digest(task), 'rubric': digest(rubric)}
        write(manifest_path, manifest)
        catalog_path = self.root / 'data/longitudinal/episodes.json'
        catalog = read(catalog_path)
        self.assertEqual(len(catalog['episodes']), 2)
        for i, original in enumerate(list(catalog['episodes'])):
            episode = copy.deepcopy(original)
            episode['id'] = f'LEGACY-SYNTHETIC-EPISODE-{i + 1}'
            catalog['episodes'].append(episode)
        write(catalog_path, catalog)
        registry = read(ROOT / 'config/agents-cli.example.json')
        agent = next(a for a in registry['agents'] if a['id']=='deepseek-v4-1-flash')
        agent.update(cli_version='0.2.0-rc.2', binary='/nonexistent/no-inference')
        self.registry = self.base / 'registry.json'
        write(self.registry, dict(registry, agents=[agent]))
        self.batch = self.base / 'formal'
        self.prepared = formal.prepare(self.root, self.registry, None, self.batch)

    def test_registration_no_model_calls_full_scope_and_no_credentials(self):
        self.assertEqual(result_exit_code(self.prepared), 0)
        self.assertEqual(self.prepared['execution_units'], 83)
        self.assertEqual(self.prepared['model_calls'], 0)
        state = formal.status(self.batch)
        self.assertTrue(state['official']); self.assertFalse(state['publication_ready'])
        self.assertEqual(state['statuses'], {'prepared':83})
        self.assertFalse(any('credentials.json' in p.name for p in self.batch.rglob('*')))
        self.assertFalse((self.batch / 'source/runs').exists())
        self.assertFalse((self.batch / 'source/data/test').exists())

    def test_source_tampering_rejected_before_client_starts(self):
        path = self.batch / 'source/benchmark/native_runtime.py'
        path.write_text(path.read_text()+'\n# changed\n')
        with patch('benchmark.formal_public.worker') as invoke:
            with self.assertRaisesRegex(ValueError, 'Frozen release changed'):
                formal.run(self.batch, self.registry)
            invoke.assert_not_called()

    def test_registry_and_plan_tampering_rejected(self):
        path = self.batch / 'agents.frozen.json'; old = path.read_bytes()
        value = read(path); value['timeout_seconds'] += 1; write(path, value)
        with self.assertRaisesRegex(ValueError, 'recipes changed'): formal.status(self.batch)
        path.write_bytes(old)
        path = self.batch / 'answers/full-batch.json'
        value = read(path); value['jobs'] = value['jobs'][1:]; write(path,value)
        with self.assertRaisesRegex(ValueError, 'capture plan changed'): formal.status(self.batch)

    def test_pending_panel_and_review_gate(self):
        out = self.base / 'board'
        result = export_leaderboard(ROOT,[self.batch],out)
        self.assertEqual(result['status'], 'formal_results_pending')
        data=read(out/'leaderboard.json')
        self.assertTrue(data['official']); self.assertFalse(data['publication_ready'])
        self.assertIsNone(data['rows'][0]['mean_score'])
        self.assertEqual(data['rows'][0]['admission_status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Finish all formal tasks'):
            formal.review(self.batch,ROOT/'config/judge.example.json',self.base/'reviews')
        # Execute the actual inline UI with a small DOM shim; no browser needed for label logic.
        if __import__('shutil').which('node'):
            script=(out/'index.html').read_text().split('<script>')[1].split('</script>')[0]
            harness="const vm=require('vm');const ids={};const node=()=>({append(){},replaceChildren(){},addEventListener(){},value:'',hidden:false});const document={getElementById:id=>ids[id]??(ids[id]=node()),createElement:node};"
            harness+='vm.runInNewContext('+json.dumps(script)+',{document});'
            harness+="if(!ids.suite.textContent.includes('正式公开评测')||!ids['scope-note'].textContent.includes('尚未完成'))process.exit(1);"
            checked=subprocess.run(['node','-e',harness],capture_output=True,text=True)
            self.assertEqual(checked.returncode,0,checked.stderr)

    def test_completed_scores_without_episode_reviews_are_not_admitted(self):
        value=formal.board(self.batch)
        value['rows'][0].update(complete=True,episodes_complete=False,mean_score=70)
        with patch('benchmark.formal_public.worker', return_value=value):
            self.assertEqual(formal.board(self.batch)['rows'][0]['admission_status'],'pending')
        value['rows'][0]['episodes_complete']=True
        with patch('benchmark.formal_public.worker', return_value=value):
            self.assertEqual(formal.board(self.batch)['rows'][0]['admission_status'],'admitted')

    def test_reuse_prepared_batch_no_relabel_and_protocol_change_rejected(self):
        adopted=self.base/'adopted'
        result=formal.prepare(self.root,self.registry,None,adopted,reuse=self.batch/'answers')
        self.assertEqual(result['reused_completed'],0)
        self.assertFalse(read(adopted/'answers/full-batch.json')['official'])
        changed=read(self.registry); changed['timeout_seconds']+=1; write(self.registry,changed)
        with self.assertRaisesRegex(ValueError,'Frozen evaluation operation failed'):
            formal.prepare(self.root,self.registry,None,self.base/'incompatible',reuse=self.batch/'answers')
        self.assertFalse((self.base/'incompatible').exists())

    def test_duplicate_or_mixed_formal_batches_rejected(self):
        with self.assertRaisesRegex(ValueError,'duplicate formal'):
            formal.export([self.batch,self.batch],self.base/'duplicate')
        with self.assertRaisesRegex(ValueError,'separately'):
            export_leaderboard(ROOT,[self.batch,self.batch/'answers'],self.base/'mixed')

    def test_multiple_configurations_and_repeats_cover_unique_tasks(self):
        reg=read(self.registry)
        reg['repeats']=2
        reg['agents'].append(dict(reg['agents'][0],id='second-configuration'))
        write(self.registry,reg)
        dest=self.base/'multiple'
        result=formal.prepare(self.root,self.registry,None,dest)
        self.assertEqual(result['execution_units'],332)
        self.assertEqual(formal.worker(dest,'verify')['verified'],True)

    def test_failed_attempts_are_not_silently_retried_by_registration(self):
        path=next((self.batch/'answers/single').glob('*/*/r1/run.json'))
        data=read(path); data['status']='failed'; write(path,data)
        with self.assertRaisesRegex(ValueError,'do not retry failed answers'):
            formal.prepare(self.root,self.registry,None,self.base/'retry',reuse=self.batch/'answers')
        self.assertFalse((self.base/'retry').exists())


if __name__=='__main__':
    unittest.main()
