import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { buildCommandArgs, helpCommandArgs } from '../commands';

test('build command preserves arguments and forwards config and flags', () => {
    assert.deepEqual(
        buildCommandArgs({
            tag: 'registry.example/app:tag with spaces',
            baseImage: 'python:3.12-slim',
            contextDir: '/tmp/project with spaces',
            configPath: '/tmp/project with spaces/pycontainer.toml',
            push: true,
            verbose: true,
            dryRun: true
        }),
        [
            '-m',
            'pycontainer.cli',
            'build',
            '--tag',
            'registry.example/app:tag with spaces',
            '--base-image',
            'python:3.12-slim',
            '--context',
            '/tmp/project with spaces',
            '--config',
            '/tmp/project with spaces/pycontainer.toml',
            '--push',
            '--verbose',
            '--dry-run'
        ]
    );
});

test('help command uses the supported package module', () => {
    assert.deepEqual(helpCommandArgs(), ['-m', 'pycontainer.cli', '--help']);
});

test('generated command runs the installed core CLI in dry-run mode', () => {
    const repositoryRoot = path.resolve(__dirname, '../../../..');
    const contextDir = path.join(repositoryRoot, 'examples', 'fastapi-app');
    const pythonPath = process.env.PYCONTAINER_PYTHON
        ?? (process.platform === 'win32' ? 'python' : 'python3');
    const pythonPathEntries = [
        path.join(repositoryRoot, 'src'),
        process.env.PYTHONPATH
    ].filter(Boolean);
    const result = spawnSync(
        pythonPath,
        buildCommandArgs({
            tag: 'extension-smoke:local',
            baseImage: 'python:3.11-slim',
            contextDir,
            dryRun: true
        }),
        {
            cwd: contextDir,
            encoding: 'utf8',
            env: {
                ...process.env,
                PYTHONPATH: pythonPathEntries.join(path.delimiter)
            }
        }
    );

    assert.equal(result.status, 0, result.error?.message ?? result.stderr);
    assert.match(result.stdout, /Build Plan for extension-smoke:local/);
});
