"""Fault injection for capture deployment: reject before stopping, restore on start failure."""
import json, os, subprocess, tempfile, unittest
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
if __name__=='__main__':unittest.main()
