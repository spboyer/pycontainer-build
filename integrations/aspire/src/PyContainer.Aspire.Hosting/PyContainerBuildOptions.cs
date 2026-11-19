namespace Aspire.Hosting;

/// <summary>
/// Optional settings for <c>WithPyContainerBuild</c>. Every value has a default inferred from the
/// Aspire resource, so most apps need none of these.
/// </summary>
public sealed class PyContainerBuildOptions
{
    /// <summary>
    /// The pycontainer command. Defaults to <c>pycontainer</c> on <c>PATH</c>.
    /// </summary>
    public string Command { get; set; } = "pycontainer";

    /// <summary>
    /// Arguments placed before <c>build</c>, for example <c>["-m", "pycontainer.cli"]</c> when
    /// <see cref="Command"/> is a Python interpreter.
    /// </summary>
    public IList<string> CommandArguments { get; } = [];

    /// <summary>
    /// Base image override. When unset, pycontainer selects <c>python:&lt;version&gt;-slim</c>
    /// from the app's <c>requires-python</c>.
    /// </summary>
    public string? BaseImage { get; set; }

    /// <summary>
    /// Target platform for the image and its dependency wheels, <c>linux/amd64</c> or <c>linux/arm64</c>.
    /// When unset, the platform requested by the Aspire compute environment is used, then <c>linux/amd64</c>.
    /// </summary>
    public string? Platform { get; set; }

    /// <summary>
    /// Stages target-compatible dependencies from <c>requirements.txt</c> or an exported <c>uv.lock</c>.
    /// </summary>
    public bool IncludeDependencies { get; set; } = true;
}
