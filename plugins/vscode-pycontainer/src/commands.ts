export interface BuildCommandOptions {
    tag: string;
    baseImage: string;
    contextDir: string;
    configPath?: string;
    push?: boolean;
    verbose?: boolean;
    dryRun?: boolean;
}

export function buildCommandArgs(options: BuildCommandOptions): string[] {
    const args = [
        '-m',
        'pycontainer.cli',
        'build',
        '--tag',
        options.tag,
        '--base-image',
        options.baseImage,
        '--context',
        options.contextDir
    ];

    if (options.configPath) {
        args.push('--config', options.configPath);
    }

    if (options.push) {
        args.push('--push');
    }

    if (options.verbose) {
        args.push('--verbose');
    }

    if (options.dryRun) {
        args.push('--dry-run');
    }

    return args;
}

export function helpCommandArgs(): string[] {
    return ['-m', 'pycontainer.cli', '--help'];
}
