using System.Globalization;
using System.Text;
using Aspire.Hosting.Publishing;
using Aspire.Hosting.Python;

namespace Aspire.Hosting.PyContainer;

internal enum DependencySource
{
    None,
    RequirementsFile,
    UvExport,
}

/// <summary>
/// Pure translation of Aspire Python metadata into a pycontainer invocation. Kept free of I/O so it can be tested.
/// </summary>
internal sealed record PyContainerBuildPlan(
    string ConfigToml,
    IReadOnlyList<string> BuildArguments,
    DependencySource DependencySource,
    string? RequirementsPath,
    string GeneratedDirectory)
{
    internal const string WorkDir = "/app";
    internal const string GeneratedRoot = ".pycontainer-aspire";

    // Host virtualenvs contain host binaries, so they must be excluded explicitly to force target-compatible
    // staging. The generated requirements directory cannot be excluded: pycontainer applies the same excludes
    // when locating the requirements file.
    private static readonly string[] s_dependencyExcludes = [".venv", "venv", "env"];

    public static PyContainerBuildPlan Create(
        string resourceName,
        string appDirectory,
        EntrypointType entrypointType,
        string entrypoint,
        string outputDirectory,
        string configPath,
        string platform,
        PyContainerBuildOptions options,
        Func<string, bool> fileExists)
    {
        var dependencySource = SelectDependencySource(appDirectory, options, fileExists);
        // Per resource, because several resources can share an app directory and build concurrently.
        var generatedDirectory = $"{GeneratedRoot}/{resourceName.ToLowerInvariant()}";
        var requirementsPath = dependencySource switch
        {
            DependencySource.RequirementsFile => "requirements.txt",
            DependencySource.UvExport => $"{generatedDirectory}/requirements.txt",
            _ => null,
        };

        var args = new List<string>(options.CommandArguments)
        {
            "build",
            "--config", configPath,
            "--context", appDirectory,
            "--tag", $"{resourceName.ToLowerInvariant()}:latest",
            "--platform", platform,
            "--no-progress",
        };

        if (!string.IsNullOrWhiteSpace(options.BaseImage))
        {
            args.Add("--base-image");
            args.Add(options.BaseImage);
        }

        if (requirementsPath is not null)
        {
            args.Add("--include-deps");
            args.Add("--requirements");
            args.Add(requirementsPath);
        }

        foreach (var exclude in s_dependencyExcludes)
        {
            args.Add("--exclude");
            args.Add(exclude);
        }

        var toml = CreateConfigToml(outputDirectory, GetImageEntrypoint(entrypointType, entrypoint));
        return new PyContainerBuildPlan(toml, args, dependencySource, requirementsPath, generatedDirectory);
    }

    /// <summary>
    /// Chooses the explicit platform, then the one requested by Aspire, then <c>linux/amd64</c>.
    /// pycontainer builds one platform per image, so multi-platform requests are rejected.
    /// </summary>
    internal static string ResolvePlatform(string? explicitPlatform, ContainerTargetPlatform? requested) =>
        !string.IsNullOrWhiteSpace(explicitPlatform) ? explicitPlatform : requested switch
        {
            null or ContainerTargetPlatform.LinuxAmd64 => "linux/amd64",
            ContainerTargetPlatform.LinuxArm64 => "linux/arm64",
            _ => throw new NotSupportedException(
                $"Target platform '{requested}' is not supported by WithPyContainerBuild; " +
                "pycontainer builds a single linux/amd64 or linux/arm64 image. Set PyContainerBuildOptions.Platform to choose one."),
        };

    /// <summary>
    /// Mirrors Aspire's generated Dockerfile entrypoints. Executables run as modules because console scripts
    /// staged on a Windows or macOS host are not runnable on a Linux target.
    /// </summary>
    internal static string[] GetImageEntrypoint(EntrypointType entrypointType, string entrypoint) => entrypointType switch
    {
        EntrypointType.Script => ["python", entrypoint.Replace('\\', '/')],
        EntrypointType.Module or EntrypointType.Executable => ["python", "-m", entrypoint],
        _ => throw new ArgumentOutOfRangeException(nameof(entrypointType), entrypointType, "Unsupported Python entrypoint type."),
    };

    private static DependencySource SelectDependencySource(string appDirectory, PyContainerBuildOptions options, Func<string, bool> fileExists)
    {
        if (!options.IncludeDependencies)
        {
            return DependencySource.None;
        }

        if (fileExists(Path.Combine(appDirectory, "requirements.txt")))
        {
            return DependencySource.RequirementsFile;
        }

        return fileExists(Path.Combine(appDirectory, "pyproject.toml"))
            ? DependencySource.UvExport
            : DependencySource.None;
    }

    private static string CreateConfigToml(string outputDirectory, IEnumerable<string> entrypoint)
    {
        var builder = new StringBuilder();
        builder.AppendLine("# Generated by PyContainer.Aspire.Hosting. Do not edit.");
        builder.AppendLine("[build]");
        builder.AppendLine(CultureInfo.InvariantCulture, $"output-dir = {TomlString(outputDirectory)}");
        builder.AppendLine(CultureInfo.InvariantCulture, $"workdir = {TomlString(WorkDir)}");
        builder.AppendLine(CultureInfo.InvariantCulture, $"entrypoint = [{string.Join(", ", entrypoint.Select(TomlString))}]");
        builder.AppendLine();
        builder.AppendLine("[build.env]");
        builder.AppendLine("PYTHONUNBUFFERED = \"1\"");
        builder.AppendLine("PYTHONDONTWRITEBYTECODE = \"1\"");
        return builder.ToString();
    }

    internal static string TomlString(string value)
    {
        var builder = new StringBuilder("\"");
        foreach (var c in value)
        {
            switch (c)
            {
                case '\\': builder.Append("\\\\"); break;
                case '"': builder.Append("\\\""); break;
                case '\n': builder.Append("\\n"); break;
                case '\r': builder.Append("\\r"); break;
                case '\t': builder.Append("\\t"); break;
                default:
                    if (char.IsControl(c))
                    {
                        builder.Append(CultureInfo.InvariantCulture, $"\\u{(int)c:X4}");
                    }
                    else
                    {
                        builder.Append(c);
                    }
                    break;
            }
        }

        return builder.Append('"').ToString();
    }
}
