"""
Shared common-word set used by scan_existing_notes.py and link_keywords.py.
Words listed here are excluded from automatic keyword extraction and auto-linking.

Supports loading additional custom filter words from a config file.
"""

# Default common-word set.
# Used to filter keywords before auto-wikilink insertion in link_keywords.py,
# preventing generic words from being linked to unrelated papers.
COMMON_WORDS = {
    # English function words
    'and', 'the', 'for', 'of', 'in', 'on', 'at', 'by', 'with', 'from',
    'to', 'as', 'or', 'but', 'not', 'a', 'an', 'is', 'are', 'was', 'were',
    'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
    'will', 'would', 'should', 'could', 'may', 'might', 'must',
    'can', 'need', 'use', 'using', 'via', 'through', 'over',
    'under', 'between', 'among', 'during', 'without', 'within',
    'this', 'that', 'these', 'those', 'it', 'its', 'they', 'their',
    'we', 'you', 'your', 'our', 'my', 'his', 'her',
    # ML/CS terms that are too common in paper titles/abstracts to be distinctive.
    # Note: 'network' and 'model' carry meaning in biology contexts (gene regulatory
    # network, disease model) so they are not filtered by default. Add them via
    # extra_common_words in the YAML config if you want to exclude them.
    'learning', 'training', 'data', 'system', 'method',
    'approach', 'framework', 'algorithm', 'task',
    # Biology/life-science terms that are too generic to distinguish individual papers.
    # As frontmatter tags these words are often shared by many papers (shared-ownership
    # cases are already filtered by link_keywords.py), but when a single paper happens
    # to be the sole owner of a generic biology tag the old logic would still mis-link
    # (e.g. linking the word "Methylation" in prose to a paper that coincidentally
    # carries a methylation tag). Adding them here resolves this in one place.
    'cell', 'cells', 'gene', 'genes', 'genome', 'genomic', 'genomics',
    'protein', 'proteins', 'peptide', 'peptides',
    'expression', 'expressed', 'transcript', 'transcripts',
    'transcription', 'transcriptional', 'transcriptomics', 'transcriptome',
    'translation', 'translational',
    'methylation', 'methylated', 'modification', 'modifications',
    'sequencing', 'sequenced', 'sequence', 'sequences',
    'mutation', 'mutations', 'mutant', 'variants', 'variant',
    'signaling', 'pathway', 'pathways', 'cascade',
    'regulation', 'regulator', 'regulators', 'regulatory',
    'function', 'functions', 'functional',
    'mechanism', 'mechanisms', 'mechanistic',
    'role', 'roles', 'study', 'studies', 'analysis', 'analyses',
    'identify', 'identifies', 'identified', 'identification',
    'reveal', 'reveals', 'revealed',
    'demonstrate', 'demonstrates', 'demonstrated', 'demonstration',
    'show', 'shows', 'showed', 'shown',
    'result', 'results', 'finding', 'findings',
    'observe', 'observed', 'observation', 'observations',
    'level', 'levels', 'change', 'changes', 'changed',
    'target', 'targets', 'targeted', 'targeting',
    'factor', 'factors', 'effector', 'effectors',
    'response', 'responses', 'responsive',
    'activity', 'activities', 'activate', 'activated', 'activation',
    'inhibit', 'inhibits', 'inhibited', 'inhibition',
    'inhibitor', 'inhibitors',
    'increase', 'increased', 'decrease', 'decreased',
    'tissue', 'tissues', 'organ', 'organs',
    'animal', 'animals', 'mouse', 'mice', 'rat', 'rats',
    'human', 'humans', 'patient', 'patients',
    'disease', 'diseases', 'disorder', 'disorders',
}


def load_extra_common_words(config_path=None):
    """
    Load additional common words from a config file and merge them into COMMON_WORDS.

    Args:
        config_path: Path to a YAML config file.
    """
    if not config_path:
        return

    try:
        import yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        extra_words = config.get('extra_common_words', [])
        if extra_words:
            COMMON_WORDS.update(w.lower() for w in extra_words)
    except Exception:
        pass  # config load failure does not affect default behavior
