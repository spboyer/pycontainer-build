import * as vscode from 'vscode';
import * as child_process from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

export function activate(context: vscode.ExtensionContext) {
    console.log('pycontainer-build extension activated');

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('pycontainer.build', () => buildContainer(false))
    );
    
    context.subscriptions.push(
        vscode.commands.registerCommand('pycontainer.buildAndPush', () => buildContainer(true))
    );
    
    context.subscriptions.push(
        vscode.commands.registerCommand('pycontainer.configure', () => configureContainer())
    );

    // Check if pycontainer-build is installed
    checkPycontainerInstalled();
}

async function buildContainer(push: boolean) {
    const config = vscode.workspace.getConfiguration('pycontainer');
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    
    if (!workspaceFolder) {
        vscode.window.showErrorMessage('No workspace folder open');
        return;
    }

    // Check for pyproject.toml
    const pyprojectPath = path.join(workspaceFolder.uri.fsPath, 'pyproject.toml');
    if (!fs.existsSync(pyprojectPath)) {
        vscode.window.showErrorMessage('No pyproject.toml found in workspace');
        return;
    }

    // Get configuration
    const tag = await vscode.window.showInputBox({
        prompt: 'Enter container image tag',
        value: config.get('defaultTag', 'myapp:latest')
    });

    if (!tag) {
        return;
    }

    const baseImage = config.get('defaultBaseImage', 'python:3.11-slim');
    const verbose = config.get('verbose', false);
    const pythonPath = config.get('pythonPath', 'python');

    // Build command arguments (using array to prevent command injection)
    const args = ['-m', 'pycontainer', 'build', '--tag', tag, '--base-image', baseImage];
    
    if (push) {
        args.push('--push');
    }
    
    if (verbose) {
        args.push('--verbose');
    }

    // Create output channel
    const outputChannel = vscode.window.createOutputChannel('pycontainer-build');
    outputChannel.show();
    outputChannel.appendLine(`Building container: ${tag}`);
    outputChannel.appendLine(`Command: ${pythonPath} ${args.join(' ')}\n`);

    // Show progress
    vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: push ? 'Building and pushing container...' : 'Building container...',
        cancellable: false
    }, async (progress) => {
        return new Promise<void>((resolve, reject) => {
            const proc = child_process.spawn(
                pythonPath,
                args,
                { cwd: workspaceFolder.uri.fsPath }
            );

            proc.stdout?.on('data', (data) => {
                outputChannel.append(data.toString());
            });

            proc.stderr?.on('data', (data) => {
                outputChannel.append(data.toString());
            });

            proc.on('error', (error) => {
                outputChannel.appendLine(`\nError: ${error.message}`);
                vscode.window.showErrorMessage(`Build failed: ${error.message}`);
                reject(error);
            });

            proc.on('close', (code) => {
                if (code !== 0) {
                    const errorMsg = `Build failed with exit code ${code}`;
                    outputChannel.appendLine(`\n${errorMsg}`);
                    vscode.window.showErrorMessage(errorMsg);
                    reject(new Error(errorMsg));
                    return;
                }
                
                const successMsg = push 
                    ? `Container built and pushed: ${tag}`
                    : `Container built: ${tag}`;
                
                outputChannel.appendLine(`\n✓ ${successMsg}`);
                vscode.window.showInformationMessage(successMsg);
                resolve();
            });
        });
    });
}

async function configureContainer() {
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    
    if (!workspaceFolder) {
        vscode.window.showErrorMessage('No workspace folder open');
        return;
    }

    const configPath = path.join(workspaceFolder.uri.fsPath, 'pycontainer.toml');
    
    // Create sample configuration
    const sampleConfig = `# pycontainer-build configuration
[build]
base_image = "python:3.11-slim"
workdir = "/app"
include_deps = true
reproducible = true

[build.labels]
maintainer = "team@example.com"

[build.env]
ENV = "production"
PORT = "8080"

[registry]
url = "ghcr.io/user/myapp"
`;

    if (fs.existsSync(configPath)) {
        const answer = await vscode.window.showWarningMessage(
            'pycontainer.toml already exists. Overwrite?',
            'Yes', 'No'
        );
        
        if (answer !== 'Yes') {
            return;
        }
    }

    fs.writeFileSync(configPath, sampleConfig);
    
    const doc = await vscode.workspace.openTextDocument(configPath);
    await vscode.window.showTextDocument(doc);
    
    vscode.window.showInformationMessage('Configuration file created');
}

async function checkPycontainerInstalled() {
    const config = vscode.workspace.getConfiguration('pycontainer');
    const pythonPath = config.get('pythonPath', 'python');
    const autoInstall = config.get('autoInstall', true);

    const proc = child_process.spawn(pythonPath, ['-m', 'pycontainer', '--help']);
    
    proc.on('error', () => {
        const message = 'pycontainer-build is not installed';
        
        if (autoInstall) {
            vscode.window.showWarningMessage(
                message + '. Install now?',
                'Yes', 'No'
            ).then(answer => {
                if (answer === 'Yes') {
                    installPycontainer(pythonPath);
                }
            });
        } else {
            vscode.window.showWarningMessage(message);
        }
    });

    proc.on('close', (code) => {
        if (code !== 0) {
            const message = 'pycontainer-build is not installed';
            
            if (autoInstall) {
                vscode.window.showWarningMessage(
                    message + '. Install now?',
                    'Yes', 'No'
                ).then(answer => {
                    if (answer === 'Yes') {
                        installPycontainer(pythonPath);
                    }
                });
            } else {
                vscode.window.showWarningMessage(message);
            }
        }
    });
}

function installPycontainer(pythonPath: string) {
    const outputChannel = vscode.window.createOutputChannel('pycontainer-build');
    outputChannel.show();
    outputChannel.appendLine('Installing pycontainer-build...\n');

    vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Installing pycontainer-build...',
        cancellable: false
    }, async () => {
        return new Promise<void>((resolve, reject) => {
            const args = ['-m', 'pip', 'install', 'pycontainer-build'];
            const proc = child_process.spawn(pythonPath, args);

            proc.stdout?.on('data', (data) => {
                outputChannel.append(data.toString());
            });

            proc.stderr?.on('data', (data) => {
                outputChannel.append(data.toString());
            });

            proc.on('error', (error) => {
                outputChannel.appendLine(`Error: ${error.message}`);
                vscode.window.showErrorMessage(`Installation failed: ${error.message}`);
                reject(error);
            });

            proc.on('close', (code) => {
                if (code !== 0) {
                    const errorMsg = `Installation failed with exit code ${code}`;
                    outputChannel.appendLine(`\n${errorMsg}`);
                    vscode.window.showErrorMessage(errorMsg);
                    reject(new Error(errorMsg));
                    return;
                }
                
                outputChannel.appendLine('\n✓ pycontainer-build installed successfully');
                vscode.window.showInformationMessage('pycontainer-build installed successfully');
                resolve();
            });
        });
    });
}

export function deactivate() {}
