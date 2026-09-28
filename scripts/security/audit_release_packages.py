"""Inspect published packages in disposable GitHub runners; never publish releases."""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, zipfile
from pathlib import Path, PurePosixPath

ROOT = Path('release-audit').resolve()
REPO = 'jintairezhao/school-watcher'
TAGS = ('v0.1.0', 'v0.2.0')

def command(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300, **kwargs)

def digest(path):
    with path.open('rb') as reader: return hashlib.file_digest(reader, 'sha256').hexdigest()

def download():
    ROOT.mkdir(exist_ok=True)
    for tag in TAGS:
        folder = ROOT / tag
        folder.mkdir(exist_ok=True)
        result = command(['gh', 'api', f'repos/{REPO}/releases/tags/{tag}'])
        if result.returncode: raise RuntimeError('Release metadata unavailable')
        release = json.loads(result.stdout)
        (folder/'metadata.json').write_text(json.dumps(release), encoding='utf-8')
        for asset in release['assets']:
            name=asset['name']
            if name != 'SHA256SUMS.txt' and not re.fullmatch(r'School-Watcher-[0-9.]+-(windows-x64-(?:portable.zip|setup.exe)|macos-(?:arm64|x64).dmg)', name):
                raise RuntimeError('Unexpected asset name')
            wanted = name=='SHA256SUMS.txt' or ('windows' in name if sys.platform=='win32' else 'macos' in name)
            if wanted:
                result=command(['gh','release','download',tag,'--repo',REPO,'--pattern',name,'--dir',str(folder)])
                if result.returncode: raise RuntimeError(f'Could not download {name}')

def inspect_tree(folder):
    files=[]; suspicious=[]; escaped=[]
    for current, dirs, names in os.walk(folder, followlinks=False):
        for name in names:
            path=Path(current)/name; relative=path.relative_to(folder).as_posix()
            if path.is_symlink():
                if not path.resolve().is_relative_to(folder.resolve()): escaped.append(relative)
                continue
            files.append({'path':relative,'size':path.stat().st_size})
            if name in ('.env','.field-key','browser-service.token') or path.suffix.lower() in ('.db','.sqlite','.sqlite3','.mp4','.mp3','.wav','.pfx','.p12'):
                suspicious.append(relative)
    return {'file_count':len(files),'files':files,'sensitive_names':suspicious,'escaping_symlinks':escaped}

def windows_signature(path):
    literal="'"+str(path).replace("'", "''")+"'"
    script='$s=Get-AuthenticodeSignature -LiteralPath '+literal+'; @{status=[string]$s.Status;subject=if($s.SignerCertificate){$s.SignerCertificate.Subject}else{$null}} | ConvertTo-Json -Compress'
    result=command(['pwsh','-NoProfile','-Command',script])
    return {'result':result.returncode,'details':result.stdout.strip()[:2000]}

def audit():
    reports=[]
    for tag in TAGS:
        folder=ROOT/tag
        release=json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        sums={line.split()[-1]:line.split()[0] for line in (folder/'SHA256SUMS.txt').read_text().splitlines()}
        for asset in release['assets']:
            package=folder/asset['name']
            if not package.is_file() or package.name=='SHA256SUMS.txt': continue
            value=digest(package)
            report={'tag':tag,'name':package.name,'size':package.stat().st_size,'sha256':value,
                    'checksum_match':value==sums.get(package.name),
                    'api_digest_match':asset.get('digest')=='sha256:'+value}
            assert report['checksum_match'] and report['api_digest_match']
            if package.suffix=='.zip':
                with zipfile.ZipFile(package) as archive:
                    report['file_count']=len(archive.infolist())
                    report['unsafe_paths']=[i.filename for i in archive.infolist() if PurePosixPath(i.filename).is_absolute() or '..' in PurePosixPath(i.filename).parts or '\\' in i.filename]
                    report['sensitive_names']=[i.filename for i in archive.infolist() if PurePosixPath(i.filename).name in ('.env','.field-key','browser-service.token') or PurePosixPath(i.filename).suffix.lower() in ('.db','.sqlite','.sqlite3','.mp4','.wav','.mp3','.pfx','.p12')]
                    inventory=[{'path':i.filename,'size':i.file_size} for i in archive.infolist()]
                (folder/(package.name+'.inventory.json')).write_text(json.dumps(inventory),encoding='utf-8')
            elif package.suffix=='.exe':
                report['signature']=windows_signature(package)
                installation=folder/'installed'
                assert installation.resolve().is_relative_to(ROOT)
                result=command([str(package),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',f'/DIR={installation}',f'/LOG={folder / "installer.log"}'])
                report['install_exit_code']=result.returncode
                if result.returncode: raise RuntimeError('Disposable installation failed')
                inventory=inspect_tree(installation)
                (folder/'installed.inventory.json').write_text(json.dumps(inventory),encoding='utf-8')
                report.update({k:v for k,v in inventory.items() if k!='files'})
                exe=installation/'SchoolWatcher.exe'
                report['application_signature']=windows_signature(exe)
                report['application_sha256']=digest(exe)
                portable=next(folder.glob('*portable.zip'))
                with zipfile.ZipFile(portable) as archive:
                    differences=[]; checked=0
                    for member in archive.infolist():
                        if member.is_dir(): continue
                        relative=PurePosixPath(member.filename)
                        assert relative.parts[0]=='SchoolWatcher' and '..' not in relative.parts
                        target=installation.joinpath(*relative.parts[1:])
                        with archive.open(member) as reader: expected=hashlib.file_digest(reader,'sha256').hexdigest()
                        if not target.is_file() or digest(target)!=expected: differences.append('/'.join(relative.parts[1:]))
                        checked+=1
                report['portable_files_compared']=checked
                report['portable_vs_installed_differences']=differences
            elif package.suffix=='.dmg':
                mount=folder/(package.stem+'-mounted');mount.mkdir()
                result=command(['hdiutil','attach','-readonly','-nobrowse','-noautoopen','-mountpoint',str(mount),str(package)])
                if result.returncode: raise RuntimeError('Cannot mount DMG read-only')
                try:
                    app=mount/'School Watcher.app'
                    inventory=inspect_tree(app)
                    (folder/(package.name+'.inventory.json')).write_text(json.dumps(inventory),encoding='utf-8')
                    report.update({k:v for k,v in inventory.items() if k!='files'})
                    for name,args in [('signature_details',['codesign','-dv','--verbose=4',str(app)]),('signature_integrity',['codesign','--verify','--deep','--strict',str(app)]),('gatekeeper',['spctl','--assess','--type','execute','--verbose=4',str(app)])]:
                        result=command(args)
                        report[name]={'exit_code':result.returncode,'output':(result.stdout+result.stderr)[:5000]}
                finally:
                    command(['hdiutil','detach',str(mount)])
            reports.append(report)
            print('AUDIT_RESULT '+json.dumps(report,ensure_ascii=False),flush=True)
    (ROOT/'report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['download','audit'])
    mode=parser.parse_args().mode
    download() if mode=='download' else audit()
