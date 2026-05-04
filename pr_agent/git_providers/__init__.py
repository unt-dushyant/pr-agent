import importlib

from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider

# Providers are imported lazily (on first use) to avoid triggering side-effects
# at module load time. For example, azure-devops SDK writes a cache directory
# during import, which fails in read-only environments like AWS Lambda.
_GIT_PROVIDER_CLASSES = {
    'github': ('pr_agent.git_providers.github_provider', 'GithubProvider'),
    'gitlab': ('pr_agent.git_providers.gitlab_provider', 'GitLabProvider'),
    'bitbucket': ('pr_agent.git_providers.bitbucket_provider', 'BitbucketProvider'),
    'bitbucket_server': ('pr_agent.git_providers.bitbucket_server_provider', 'BitbucketServerProvider'),
    'azure': ('pr_agent.git_providers.azuredevops_provider', 'AzureDevopsProvider'),
    'codecommit': ('pr_agent.git_providers.codecommit_provider', 'CodeCommitProvider'),
    'local': ('pr_agent.git_providers.local_git_provider', 'LocalGitProvider'),
    'gerrit': ('pr_agent.git_providers.gerrit_provider', 'GerritProvider'),
    'gitea': ('pr_agent.git_providers.gitea_provider', 'GiteaProvider'),
}

_GIT_PROVIDERS: dict = {}

# Reverse map: class name → provider id, used by __getattr__ below.
_CLASS_NAME_TO_PROVIDER = {
    class_name: provider_id
    for provider_id, (_, class_name) in _GIT_PROVIDER_CLASSES.items()
}


def __getattr__(name: str):
    """Lazily resolve provider class names exported from this package.

    Allows ``from pr_agent.git_providers import AzureDevopsProvider`` (and
    other provider classes) without importing them at module load time.
    """
    if name in _CLASS_NAME_TO_PROVIDER:
        return _load_provider(_CLASS_NAME_TO_PROVIDER[name])
    raise AttributeError(f"module 'pr_agent.git_providers' has no attribute {name!r}")


def _load_provider(provider_id: str):
    if provider_id not in _GIT_PROVIDERS:
        if provider_id not in _GIT_PROVIDER_CLASSES:
            raise ValueError(f"Unknown git provider: {provider_id}")
        module_path, class_name = _GIT_PROVIDER_CLASSES[provider_id]
        module = importlib.import_module(module_path)
        _GIT_PROVIDERS[provider_id] = getattr(module, class_name)
    return _GIT_PROVIDERS[provider_id]


def get_git_provider():
    try:
        provider_id = get_settings().config.git_provider
    except AttributeError as e:
        raise ValueError("git_provider is a required attribute in the configuration file") from e
    if provider_id not in _GIT_PROVIDER_CLASSES:
        raise ValueError(f"Unknown git provider: {provider_id}")
    return _load_provider(provider_id)


def get_git_provider_with_context(pr_url) -> GitProvider:
    """
    Get a GitProvider instance for the given PR URL. If the GitProvider instance is already in the context, return it.
    """

    is_context_env = None
    try:
        is_context_env = context.get("settings", None)
    except Exception:
        pass  # we are not in a context environment (CLI)

    # check if context["git_provider"]["pr_url"] exists
    if is_context_env and context.get("git_provider", {}).get("pr_url", {}):
        git_provider = context["git_provider"]["pr_url"]
        # possibly check if the git_provider is still valid, or if some reset is needed
        # ...
        return git_provider
    else:
        try:
            provider_id = get_settings().config.git_provider
            if provider_id not in _GIT_PROVIDER_CLASSES:
                raise ValueError(f"Unknown git provider: {provider_id}")
            git_provider = _load_provider(provider_id)(pr_url)
            if is_context_env:
                context["git_provider"] = {pr_url: git_provider}
            return git_provider
        except Exception as e:
            raise ValueError(f"Failed to get git provider for {pr_url}") from e
