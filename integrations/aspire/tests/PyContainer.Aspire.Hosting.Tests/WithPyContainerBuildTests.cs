using Aspire.Hosting;
using Aspire.Hosting.ApplicationModel;
using Aspire.Hosting.Eventing;
using Aspire.Hosting.Pipelines;
using Aspire.Hosting.PyContainer;
using Aspire.Hosting.Python;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace PyContainer.Aspire.Hosting.Tests;

public sealed class WithPyContainerBuildTests : IDisposable
{
    private readonly string _appDirectory = Directory.CreateTempSubdirectory("pycontainer-aspire-").FullName;

    public WithPyContainerBuildTests() => File.WriteAllText(Path.Combine(_appDirectory, "main.py"), "print('hi')");

    public void Dispose() => Directory.Delete(_appDirectory, recursive: true);

    [Fact]
    public void RunModeLeavesResourceUnchanged()
    {
        var builder = DistributedApplication.CreateBuilder([]);
        var app = builder.AddPythonApp("api", _appDirectory, "main.py");
        var before = app.Resource.Annotations.Count;

        app.WithPyContainerBuild();

        Assert.Equal(before, app.Resource.Annotations.Count);
        Assert.False(app.Resource.TryGetLastAnnotation<PyContainerBuildAnnotation>(out _));
    }

    [Theory]
    [InlineData("script")]
    [InlineData("module")]
    [InlineData("uvicorn")]
    public void ReadsAspireEntrypointMetadata(string kind)
    {
        var builder = DistributedApplication.CreateBuilder([]);
        IResource resource = kind switch
        {
            "script" => builder.AddPythonApp("api", _appDirectory, "main.py").Resource,
            "module" => builder.AddPythonModule("api", _appDirectory, "flask").Resource,
            _ => builder.AddUvicornApp("api", _appDirectory, "main:app").Resource,
        };

        var expected = kind switch
        {
            "script" => (EntrypointType.Script, "main.py"),
            "module" => (EntrypointType.Module, "flask"),
            _ => (EntrypointType.Executable, "uvicorn"),
        };
        Assert.Equal(expected, PythonEntrypointReader.Read(resource));
    }

    [Fact]
    public async Task PublishModeReplacesBuildAndPushStepsAndKeepsTheirShape()
    {
        var output = Directory.CreateTempSubdirectory("pycontainer-aspire-out-").FullName;
        try
        {
            var builder = DistributedApplication.CreateBuilder(["--operation", "publish", "--publisher", "default", "--output-path", output]);
            builder.AddPythonApp("api", _appDirectory, "main.py")
                .WithPyContainerBuild()
                .WithPyContainerBuild(o => o.Platform = "linux/arm64");

            using var app = builder.Build();
            var model = app.Services.GetRequiredService<DistributedApplicationModel>();
            var resource = model.Resources.Single(r => r.Name == "api");

            var originalSteps = await CreateStepsAsync(app, model, resource);
            var originalBuild = originalSteps.Single(s => s.Name == "build-api");
            var originalPush = originalSteps.Single(s => s.Name == "push-api");

            await app.Services.GetRequiredService<IDistributedApplicationEventing>()
                .PublishAsync(new BeforeStartEvent(app.Services, model));

            var steps = await CreateStepsAsync(app, model, resource);

            Assert.Single(resource.Annotations.OfType<PipelineStepAnnotation>());
            Assert.Equal(originalSteps.Select(s => s.Name), steps.Select(s => s.Name));
            Assert.Contains(WellKnownPipelineSteps.CheckContainerRuntime, originalBuild.DependsOnSteps);
            AssertReplaced(originalBuild, steps.Single(s => s.Name == "build-api"));
            AssertReplaced(originalPush, steps.Single(s => s.Name == "push-api"));
            Assert.Equal("linux/arm64", resource.Annotations.OfType<PyContainerBuildAnnotation>().Single().Options.Platform);
        }
        finally
        {
            Directory.Delete(output, recursive: true);
        }
    }

    private static void AssertReplaced(PipelineStep original, PipelineStep replacement)
    {
        Assert.NotSame(original.Action, replacement.Action);
        Assert.Contains("pycontainer", replacement.Description);
        Assert.Equal(original.Tags, replacement.Tags);
        Assert.Equal(original.DependsOnSteps.Where(s => s != WellKnownPipelineSteps.CheckContainerRuntime), replacement.DependsOnSteps);
        Assert.Equal(original.RequiredBySteps, replacement.RequiredBySteps);
        Assert.Same(original.Resource, replacement.Resource);
    }

    private static async Task<List<PipelineStep>> CreateStepsAsync(DistributedApplication app, DistributedApplicationModel model, IResource resource)
    {
        var context = new PipelineStepFactoryContext
        {
            PipelineContext = new PipelineContext(
                model,
                app.Services.GetRequiredService<DistributedApplicationExecutionContext>(),
                app.Services,
                NullLogger.Instance,
                CancellationToken.None),
            Resource = resource,
        };

        var steps = new List<PipelineStep>();
        foreach (var annotation in resource.Annotations.OfType<PipelineStepAnnotation>())
        {
            steps.AddRange(await annotation.CreateStepsAsync(context));
        }

        return steps;
    }
}
