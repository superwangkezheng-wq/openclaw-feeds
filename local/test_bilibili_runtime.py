"""Fault injection for capture deployment: reject before stopping, restore on start failure."""
import json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path

SCRIPT=Path(__file__).with_name('bilibili_capture.sh')
class RuntimeAdmission(unittest.TestCase):
    def exercise(self,mode):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);envfile=root/'bilibili.env';envfile.write_text('BILIBILI_UID=1\nBILIBILI_SESSDATA=dummy\n');log=root/'docker.jsonl'
            fake=root/'docker';fake.write_text('''#!/opt/homebrew/bin/python3.11
import json,os,sys
args=sys.argv[1:]
with open(os.environ['TEST_DOCKER_LOG'],'a') as f:f.write(json.dumps(args)+'\\n')
mode=os.environ['TEST_DOCKER_MODE']
if args[:2]==['image','inspect'] and mode=='image':sys.exit(1)
if args[0]=='pull':sys.exit(43)
if args[:2]==['run','--rm'] and mode=='browser':sys.exit(44)
if args[:2]==['run','-d'] and mode=='start':sys.exit(45)
sys.exit(0)
''');fake.chmod(0o700)
            env={**os.environ,'PATH':str(root)+':/opt/homebrew/bin:/usr/bin:/bin','BILIBILI_ENV_FILE':str(envfile),'TEST_DOCKER_MODE':mode,'TEST_DOCKER_LOG':str(log)}
            result=subprocess.run(['/bin/zsh',str(SCRIPT),'--start-container'],env=env,capture_output=True,text=True,timeout=10)
            return result,[json.loads(line) for line in log.read_text().splitlines()]
    def test_image_or_browser_rejection_does_not_touch_serving_container(self):
        for mode in ('image','browser'):
            result,calls=self.exercise(mode)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(any(args[0] in ('stop','rename') or args[:2]==['rm','-f'] for args in calls))
    def test_start_failure_restores_previous_container(self):
        result,calls=self.exercise('start');self.assertNotEqual(result.returncode,0)
        self.assertTrue(any(args[0]=='stop' for args in calls))
        renames=[args for args in calls if args[0]=='rename'];self.assertEqual(len(renames),2)
        self.assertEqual(renames[0][1],renames[1][2]);self.assertEqual(renames[0][2],renames[1][1])
        self.assertEqual(calls[-1],['start','openclaw-rsshub'])
class CaptureIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'local').mkdir();(self.root/'feeds/bilibili').mkdir(parents=True)
        self.script=self.root/'local/bilibili_capture.sh';shutil.copyfile(SCRIPT,self.script)
        self.bin=self.root/'bin';self.bin.mkdir();self.status=self.root/'status.json'
        envfile=self.root/'bilibili.env';envfile.write_text('BILIBILI_UID=1\nBILIBILI_SESSDATA=dummy\n')
        self.env={**os.environ,'PATH':str(self.bin)+':/opt/homebrew/bin:/usr/bin:/bin','BILIBILI_ENV_FILE':str(envfile),'BILIBILI_STATUS_FILE':str(self.status),'TEST_ROOT':str(self.root)}
        for uid in ('73414544','519463151','1567748478'):(self.root/f'feeds/bilibili/{uid}.xml').write_text('old-'+uid)
        self.fake('curl',r'''
import os,sys
from pathlib import Path
args=sys.argv[1:];url=args[-1]
if '/bilibili/' not in url:sys.exit(0)
uid=url.rsplit('/',1)[-1]
if uid in os.environ.get('TEST_FAILED','').split(','):sys.exit(22)
Path(args[args.index('-o')+1]).write_text('<rss><channel><item><title>'+uid+'</title><description>'+'x'*500+'</description></item></channel></rss>')
''')
        self.fake('git',r'''
import json,os,sys
from pathlib import Path
root=Path(os.environ['TEST_ROOT']);args=sys.argv[1:]
with (root/'git.jsonl').open('a') as f:f.write(json.dumps(args)+'\n')
if args[0]=='status':
 if not (root/'committed').exists():print(' M feeds/bilibili/73414544.xml')
elif 'commit' in args:(root/'committed').touch();(root/'ahead').touch()
elif args[0]=='rev-list':print(1 if (root/'ahead').exists() else 0)
elif args[0]=='push':
 if os.environ.get('TEST_PUSH_FAIL')=='1':sys.exit(1)
 (root/'ahead').unlink(missing_ok=True)
''')
    def fake(self,name,body):
        file=self.bin/name;file.write_text('#!/opt/homebrew/bin/python3.11\n'+body);file.chmod(0o700)
    def capture(self,**extra):
        result=subprocess.run(['/bin/zsh',str(self.script)],env={**self.env,**extra},capture_output=True,text=True,timeout=25)
        return result,json.loads(self.status.read_text())
    def test_partial_source_failure_publishes_valid_peers_and_preserves_failed_feed(self):
        result,status=self.capture(TEST_FAILED='1567748478')
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(status['result'],'warn');self.assertTrue(status['published'])
        self.assertEqual(status['failedSources'],['1567748478']);self.assertEqual(len(status['fetchedSources']),2)
        self.assertEqual((self.root/'feeds/bilibili/1567748478.xml').read_text(),'old-1567748478')
        self.assertIn(['push','-q'],[json.loads(x) for x in (self.root/'git.jsonl').read_text().splitlines()])
    def test_pending_push_is_retried_even_when_next_capture_has_no_diff(self):
        result,status=self.capture(TEST_PUSH_FAIL='1');self.assertNotEqual(result.returncode,0);self.assertEqual(status['result'],'fail');self.assertEqual(status['detail'],'git_push_failed')
        result,status=self.capture();self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(status['result'],'ok')
        calls=[json.loads(x) for x in (self.root/'git.jsonl').read_text().splitlines()]
        self.assertEqual(sum(args==['push','-q'] for args in calls),2);self.assertEqual(sum('commit' in args for args in calls),1)
    def test_all_failed_sources_never_publish_or_replace_last_good_content(self):
        result,status=self.capture(TEST_FAILED='73414544,519463151,1567748478')
        self.assertNotEqual(result.returncode,0);self.assertEqual(status['result'],'fail');self.assertFalse(status['published']);self.assertFalse((self.root/'git.jsonl').exists())
        for uid in status['failedSources']:self.assertEqual((self.root/f'feeds/bilibili/{uid}.xml').read_text(),'old-'+uid)
    def test_persistent_partial_failure_escalates_after_72_hours(self):
        self.status.write_text(json.dumps({'failedSinceBySource':{'1567748478':'2026-01-01T00:00:00Z'}}))
        result,status=self.capture(TEST_FAILED='1567748478');self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(status['result'],'fail');self.assertTrue(status['published']);self.assertEqual(status['rootCauseIds'],['feeds/source/1567748478'])

if __name__=='__main__':unittest.main()
