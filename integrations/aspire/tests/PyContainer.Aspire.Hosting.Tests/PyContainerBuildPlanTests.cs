using Aspire.Hosting;
using Aspire.Hosting.PyContainer;
using Aspire.Hosting.Publishing;
using Aspire.Hosting.Python;
using Xunit;

namespace PyContainer.Aspire.Hosting.Tests;

public class PyContainerBuildPlanTests
{
    private const string AppDirectory = @"C:\src\my app";

    private static PyContainerBuildPlan Create(
        PyContainerBuildOptions? options = null,
        EntrypointType type = EntrypointType.Script,
        string entrypoint = "main.py",
        params string[] existingFiles) =>
        PyContainerBuildPlan.Create(
            "Api", AppDirectory, type, entrypoint, @"C:\host\obj\pycontainer\api\image", @"C:\host\obj\pycontainer\api\pycontainer.toml",
            "linux/amd64",
            options ?? new PyContainerBuildOptions(),
            path => existingFiles.Contains(Path.GetFileName(path)));

    [Fact]
    public void BuildsCommandWithLowercaseTagPlatformAndVenvExcludes()
    {
        var options = new PyContainerBuildOptions { Command = "python", BaseImage = "python:3.12-slim" };
        options.CommandArguments.Add("-m");
        options.CommandArguments.Add("pycontainer.cli");

        var plan = Create(options);

        Assert.Equal(
            [
                "-m", "pycontainer.cli", "build",
                "--config", @"C:\host\obj\pycontainer\api\pycontainer.toml",
                "--context", AppDirectory,
                "--tag", "api:latest",
                "--platform", "linux/amd64",
                "--no-progress",
                "--base-image", "python:3.12-slim",
                "--exclude", ".venv", "--exclude", "venv", "--exclude", "env",
            ],
            plan.BuildArguments);
    }

    [Fact]
    public void ConfigEscapesWindowsOutputPathAndSetsEntrypoint()
    {
        var plan = Create(type: EntrypointType.Script, entrypoint: @"src\main.py");

        Assert.Contains(@"output-dir = ""C:\\host\\obj\\pycontainer\\api\\image""", plan.ConfigToml);
        Assert.Contains(@"entrypoint = [""python"", ""src/main.py""]", plan.ConfigToml);
        Assert.Contains(@"workdir = ""/app""", plan.ConfigToml);
    }

    [Theory]
    [InlineData(EntrypointType.Script, "app.py", new[] { "python", "app.py" })]
    [InlineData(EntrypointType.Module, "flask", new[] { "python", "-m", "flask" })]
    [InlineData(EntrypointType.Executable, "uvicorn", new[] { "python", "-m", "uvicorn" })]
    public void MapsEntrypointsLikeGeneratedDockerfile(EntrypointType type, string entrypoint, string[] expected) =>
        Assert.Equal(expected, PyContainerBuildPlan.GetImageEntrypoint(type, entrypoint));

    [Fact]
    public void PrefersRequirementsFile()
    {
        var plan = Create(existingFiles: ["requirements.txt", "pyproject.toml"]);

        Assert.Equal(DependencySource.RequirementsFile, plan.DependencySource);
        Assert.Equal(["--include-deps", "--requirements", "requirements.txt"], FindSequence(plan.BuildArguments, "--include-deps", 3));
    }

    [Fact]
    public void ExportsPyprojectDependenciesWithUv()
    {
        var plan = Create(existingFiles: ["pyproject.toml"]);

        Assert.Equal(DependencySource.UvExport, plan.DependencySource);
        Assert.Equal(".pycontainer-aspire/api/requirements.txt", plan.RequirementsPath);
        Assert.Equal(".pycontainer-aspire/api", plan.GeneratedDirectory);
    }

    [Fact]
    public void OmitsDependenciesWhenNoManifestOrDisabled()
    {
        Assert.Equal(DependencySource.None, Create().DependencySource);

        var disabled = Create(new PyContainerBuildOptions { IncludeDependencies = false }, existingFiles: ["requirements.txt"]);
        Assert.Equal(DependencySource.None, disabled.DependencySource);
        Assert.DoesNotContain("--include-deps", disabled.BuildArguments);
    }

    [Fact]
    public void TomlStringEscapesControlCharacters() =>
        Assert.Equal(@"""a\""b\nc\u0001""", PyContainerBuildPlan.TomlString("a\"b\nc\u0001"));

    [Theory]
    [InlineData("linux/arm64", ContainerTargetPlatform.LinuxAmd64, "linux/arm64")]
    [InlineData(null, ContainerTargetPlatform.LinuxArm64, "linux/arm64")]
    [InlineData(null, null, "linux/amd64")]
    public void ResolvesPlatformFromOptionsThenAspire(string? explicitPlatform, ContainerTargetPlatform? requested, string expected) =>
        Assert.Equal(expected, PyContainerBuildPlan.ResolvePlatform(explicitPlatform, requested));

    [Fact]
    public void RejectsMultiPlatformRequests() =>
        Assert.Throws<NotSupportedException>(() => PyContainerBuildPlan.ResolvePlatform(null, ContainerTargetPlatform.AllLinux));

    private static string[] FindSequence(IReadOnlyList<string> values, string start, int count)
    {
        var index = values.ToList().IndexOf(start);
        Assert.True(index >= 0, $"'{start}' not found.");
        return values.Skip(index).Take(count).ToArray();
    }
}
