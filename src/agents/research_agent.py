import json
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Callable

from duckduckgo_search import DDGS
from strands import tool

from src.agents.core.base_agent import BaseGameAgent
from src.core.types import AgentContext


RESEARCH_AGENT_SYSTEM_PROMPT = """You are a Research Agent that gathers reference material for world-building.

## TOOLS
- **web_search(query, max_results)** — DuckDuckGo search. Returns titles, URLs, snippets.
- **fetch_page(url)** — Fetch a web page as plain text (truncated to fit context).

## STRATEGY
1. Use `web_search` to find relevant pages (Wikipedia, fan wikis like Wookieepedia/UESP/etc.)
2. Use `fetch_page` on the 2-3 best URLs. **Limit to 2-3 fetches** to avoid context overflow.
3. Compile findings into your response.

For Wikipedia, fetch directly: `fetch_page("https://en.wikipedia.org/wiki/Topic_Name")`

## RESPONSE FORMAT
Return material organized by source with relevant extracts covering:
names, political structures, events, factions, notable figures, cultural details, terminology.

Skip meta content (references, navigation, edit history).

## IMPORTANT
- Fetch at most 2-3 pages to stay within token limits
- Summarize and extract key details rather than dumping raw text
- If you can't find material, say so clearly
"""


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default 5).

    Returns:
        Formatted search results with titles, URLs, and snippets.
    """
    try:
        results = DDGS().text(query, max_results=max_results, region="wt-wt")
    except Exception as e:
        return f"Search failed: {e}"

    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("href", "")
        snippet = r.get("body", "")
        lines.append(f"{i}. **{title}**\n   URL: {url}\n   {snippet}")
    return "\n\n".join(lines)


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")

_MAX_PAGE_CHARS = 6_000

_WIKI_PATTERN = re.compile(r"https?://([a-z]{2,})\.wikipedia\.org/wiki/(.+)")


def _fetch_wikipedia(lang: str, title: str) -> str:
    """Fetch a Wikipedia article via the MediaWiki API (plain text)."""
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": title.replace("_", " "),
        "prop": "extracts",
        "explaintext": "1",
        "format": "json",
        "formatversion": "2",
    })
    api_url = f"https://{lang}.wikipedia.org/w/api.php?{params}"
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "ForgeRPG/1.0 (worldbuilding research agent)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    pages = data.get("query", {}).get("pages", [])
    if pages:
        return pages[0].get("extract", "")
    return ""


def _fetch_with_curl(url: str) -> str:
    """Fetch a URL using curl (bypasses TLS fingerprint blocking)."""
    result = subprocess.run(
        ["curl", "-sL", "--max-time", "15",
         "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
         "-H", "Accept: text/html,application/xhtml+xml",
         "-H", "Accept-Language: en-US,en;q=0.9",
         url],
        capture_output=True, timeout=20,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"curl failed (exit {result.returncode}): {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


@tool
def fetch_page(url: str) -> str:
    """Fetch a web page and return its content as plain text.

    Args:
        url: The URL to fetch.

    Returns:
        Plain text content of the page, truncated to ~6000 characters.
    """
    try:
        # Use Wikipedia API for wikipedia.org URLs (structured plain text)
        wiki_match = _WIKI_PATTERN.match(url)
        if wiki_match:
            text = _fetch_wikipedia(wiki_match.group(1), wiki_match.group(2))
        else:
            html = _fetch_with_curl(url)
            text = _HTML_TAG_RE.sub("", html)
            text = _WHITESPACE_RE.sub("\n\n", text).strip()
    except Exception as e:
        return f"Failed to fetch {url}: {e}"

    if len(text) > _MAX_PAGE_CHARS:
        text = text[:_MAX_PAGE_CHARS] + "\n\n[...truncated]"
    return text


RESEARCH_TOOLS: list[Callable] = [
    web_search,
    fetch_page,
]


class ResearchAgent(BaseGameAgent):
    """Research agent for gathering reference material from the web.

    Used by WorldForge to research real-world history, mythology,
    fictional universes, and other reference material for world-building.
    """

    AGENT_NAME = "research_agent"
    DEFAULT_TOOLS = RESEARCH_TOOLS

    def __init__(self, context_or_session_id: AgentContext | str, callback_handler: Any = None):
        """Initialize the Research Agent.

        Args:
            context_or_session_id: Either an AgentContext or session_id string.
            callback_handler: Optional callback handler.
        """
        if isinstance(context_or_session_id, str):
            context = AgentContext(
                player_id=context_or_session_id,
                session_id=context_or_session_id,
                callback_handler=callback_handler,
            )
        else:
            context = context_or_session_id

        super().__init__(context)

    def _get_session_id(self) -> str:
        """Use context session ID."""
        return self.context.session_id

    def _build_system_prompt(self) -> str:
        """Return the research agent system prompt."""
        return RESEARCH_AGENT_SYSTEM_PROMPT

    def _build_context(self, user_input: str) -> str:
        """No additional context needed for research queries."""
        return user_input

    def research(self, query: str) -> str:
        """Execute a research query.

        Args:
            query: The research topic or question.

        Returns:
            Research findings as formatted text.
        """
        return self.process(query)
