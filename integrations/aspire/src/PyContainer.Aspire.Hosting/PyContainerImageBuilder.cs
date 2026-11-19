using System.Diagnostics;
using System.Text;
using Aspire.Hosting.ApplicationModel;
using Aspire.Hosting.Pipelines;
using Aspire.Hosting.Publishing;
using Aspire.Hosting.Python;
using Microsoft.Extensions.Logging;

namespace Aspire.Hosting.PyContainer;

internal static class PyContainerImageBuilder
{
    public static async Task BuildAsync(
        PythonAppResource resource,
        IResource target,
        string appHostDirectory,
        bool push,
        PipelineStepContext context)
    {
        var options = resource.TryGetLastAnnotation<PyContainerBuildAnnotation>(out var annotation)
            ? annotation.Options
            : new PyContainerBuildOptions();

        if (resource.Annotations.OfType<ContainerFilesDestinationAnnotation>().Any())
        {
            throw new NotSupportedException(
                $"'{resource.Name}' copies files from other container images (container files). " +
                "WithPyContainerBuild does not support that yet; remove WithPyContainerBuild to use the Dockerfile build.");
        }

        var (entrypointType, entrypoint) = PythonEntrypointReader.Read(resource);
        var buildOptions = await GetAspireBuildOptionsAsync(target, context).ConfigureAwait(false);
        if (buildOptions.Destination == ContainerImageDestination.Archive)
        {
            throw new NotSupportedException(
                $"'{resource.Name}' is configured to save its image as an archive, which WithPyContainerBuild does not support.");
        }

        var platform = PyContainerBuildPlan.ResolvePlatform(options.Platform, buildOptions.TargetPlatform);
        var appDirectory = resource.WorkingDirectory;
        var workRoot = Path.Combine(appHostDirectory, "obj", "pycontainer", resource.Name);
        var configPath = Path.Combine(workRoot, "pycontainer.toml");
        var outputDirectory = Path.Combine(workRoot, "image");

        var plan = PyContainerBuildPlan.Create(
            resource.Name, appDirectory, entrypointType, entrypoint, outputDirectory, configPath, platform, options, File.Exists);

        var arguments = plan.BuildArguments.ToList();
        string description;
        if (push)
        {
            var remoteImage = await ResolveRemoteImageAsync(target, context).ConfigureAwait(false);
            arguments.Add("--push");
            arguments.Add("--registry");
            arguments.Add(remoteImage);
            description = $"Building and pushing **{resource.Name}** to `{remoteImage}` with pycontainer";
        }
        else
        {
            description = $"Building **{resource.Name}** OCI image with pycontainer";
        }

        Directory.CreateDirectory(workRoot);
        await File.WriteAllTextAsync(configPath, plan.ConfigToml, context.CancellationToken).ConfigureAwait(false);

        var task = await context.ReportingStep.CreateTaskAsync(description, context.CancellationToken).ConfigureAwait(false);
        await using (task.ConfigureAwait(false))
        {
            var generatedDirectory = Path.Combine(appDirectory, plan.GeneratedDirectory);
            try
            {
                if (plan.DependencySource == DependencySource.UvExport)
                {
                    await ExportUvRequirementsAsync(appDirectory, generatedDirectory, context).ConfigureAwait(false);
                }

                await ProcessRunner.RunAsync(options.Command, arguments, appDirectory, context.Logger, context.CancellationToken).ConfigureAwait(false);

                var result = push ? $"Pushed **{resource.Name}** with pycontainer" : $"Built **{resource.Name}** at `{outputDirectory}`";
                await task.CompleteAsync(result, CompletionState.Completed, context.CancellationToken).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await task.CompleteAsync($"pycontainer failed for **{resource.Name}**: {ex.Message}", CompletionState.CompletedWithError, context.CancellationToken).ConfigureAwait(false);
                throw;
            }
            finally
            {
                if (plan.DependencySource == DependencySource.UvExport)
                {
                    RemoveGeneratedFiles(appDirectory, generatedDirectory, context.Logger);
                }
            }
        }
    }

    private static void RemoveGeneratedFiles(string appDirectory, string generatedDirectory, ILogger logger)
    {
        if (Directory.Exists(generatedDirectory))
        {
            Directory.Delete(generatedDirectory, recursive: true);
        }

        var root = Path.Combine(appDirectory, PyContainerBuildPlan.GeneratedRoot);
        if (Directory.Exists(root) && !Directory.EnumerateFileSystemEntries(root).Any())
        {
            try
            {
                Directory.Delete(root);
            }
            catch (IOException ex)
            {
                // Another resource sharing this app directory started exporting; it removes the root when done.
                logger.LogDebug(ex, "Left {Directory} in place because it is in use.", root);
            }
        }
    }

    private static async Task ExportUvRequirementsAsync(string appDirectory, string generatedDirectory, PipelineStepContext context)
    {
        Directory.CreateDirectory(generatedDirectory);
        List<string> arguments =
        [
            "export", "--quiet", "--format", "requirements-txt", "--no-dev", "--no-hashes", "--no-emit-project", "--no-header",
            "--output-file", Path.Combine(generatedDirectory, "requirements.txt"),
        ];

        if (File.Exists(Path.Combine(appDirectory, "uv.lock")))
        {
            arguments.Add("--frozen");
        }

        await ProcessRunner.RunAsync("uv", arguments, appDirectory, context.Logger, context.CancellationToken).ConfigureAwait(false);
    }

    // Aspire's own evaluation of these callbacks is internal; run them the same way through the public annotation.
    private static async Task<ContainerBuildOptionsCallbackContext> GetAspireBuildOptionsAsync(IResource target, PipelineStepContext context)
    {
        var buildOptions = new ContainerBuildOptionsCallbackContext(
            target, context.Services, context.Logger, context.CancellationToken, context.ExecutionContext);
        foreach (var callback in target.Annotations.OfType<ContainerBuildOptionsCallbackAnnotation>())
        {
            await callback.Callback(buildOptions).ConfigureAwait(false);
        }

        return buildOptions;
    }

    private static async Task<string> ResolveRemoteImageAsync(IResource target, PipelineStepContext context)
    {
        var registry = GetContainerRegistry(target);
        var endpoint = await registry.Endpoint.GetValueAsync(context.CancellationToken).ConfigureAwait(false);
        if (string.IsNullOrEmpty(endpoint))
        {
            throw new InvalidOperationException(
                $"'{target.Name}' targets a local container registry. pycontainer pushes to remote registries only; " +
                "add a remote registry (for example builder.AddContainerRegistry(...)).");
        }

        IValueProvider imageReference = new ContainerImageReference(target);
        var valueContext = new ValueProviderContext { ExecutionContext = context.ExecutionContext };
        return await imageReference.GetValueAsync(valueContext, context.CancellationToken).ConfigureAwait(false)
            ?? throw new InvalidOperationException($"Could not resolve the remote image name for '{target.Name}'.");
    }

    // Mirrors Aspire's registry resolution order for image pushes.
    private static IContainerRegistry GetContainerRegistry(IResource resource)
    {
        if (resource.Annotations.OfType<ContainerRegistryReferenceAnnotation>().LastOrDefault() is { } reference)
        {
            return reference.Registry;
        }

        if (resource.GetDeploymentTargetAnnotation()?.ContainerRegistry is { } deploymentRegistry)
        {
            return deploymentRegistry;
        }

        var targets = resource.Annotations.OfType<RegistryTargetAnnotation>().ToArray();
        return targets.Length switch
        {
            1 => targets[0].Registry,
            0 => throw new InvalidOperationException($"'{resource.Name}' has no container registry to push to."),
            _ => throw new InvalidOperationException(
                $"'{resource.Name}' has multiple container registries; choose one with WithContainerRegistry(...)."),
        };
    }
}

internal static class PythonEntrypointReader
{
    // Aspire.Hosting.Python keeps this annotation internal; read it by shape and fail loudly if it changes.
    public static (EntrypointType Type, string Entrypoint) Read(IResource resource)
    {
        var annotation = resource.Annotations.LastOrDefault(a => a.GetType().Name == "PythonEntrypointAnnotation");
        var type = annotation?.GetType().GetProperty("Type")?.GetValue(annotation);
        var entrypoint = annotation?.GetType().GetProperty("Entrypoint")?.GetValue(annotation);

        if (type is EntrypointType entrypointType && entrypoint is string value && value.Length > 0)
        {
            return (entrypointType, value);
        }

        throw new InvalidOperationException(
            $"Could not read the Python entrypoint for '{resource.Name}'. " +
            "This Aspire.Hosting.Python version is not supported by WithPyContainerBuild.");
    }
}

internal static class ProcessRunner
{
    private const int MaxErrorLines = 20;

    public static async Task RunAsync(
        string command,
        IEnumerable<string> arguments,
        string workingDirectory,
        ILogger logger,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo(command)
        {
            WorkingDirectory = workingDirectory,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
            UseShellExecute = false,
        };
        // Redirected Python stdio otherwise uses the ANSI code page on Windows and fails on non-ASCII output.
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
        startInfo.Environment["PYTHONUTF8"] = "1";
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        var errorTail = new Queue<string>();
        using var process = new Process { StartInfo = startInfo };
        process.OutputDataReceived += (_, e) => { if (e.Data is not null) logger.LogInformation("{Output}", e.Data); };
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is null)
            {
                return;
            }

            // Tools such as uv report progress on stderr, so it is not treated as a warning by itself.
            logger.LogInformation("{Output}", e.Data);
            lock (errorTail)
            {
                errorTail.Enqueue(e.Data);
                if (errorTail.Count > MaxErrorLines)
                {
                    errorTail.Dequeue();
                }
            }
        };

        try
        {
            process.Start();
        }
        catch (System.ComponentModel.Win32Exception ex)
        {
            throw new InvalidOperationException(
                $"Could not start '{command}'. Install it and make sure it is on PATH.", ex);
        }

        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        try
        {
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            throw;
        }

        if (process.ExitCode != 0)
        {
            string details;
            lock (errorTail)
            {
                details = string.Join(Environment.NewLine, errorTail);
            }

            throw new InvalidOperationException(
                $"'{command}' exited with code {process.ExitCode}." +
                (details.Length > 0 ? Environment.NewLine + details : string.Empty));
        }
    }
}
