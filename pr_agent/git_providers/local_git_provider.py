import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import List

from git import GitCommandError, Repo

from pr_agent.config_loader import _find_repository_root, settings
from pr_agent.git_providers.git_provider import EDIT_TYPE, FilePatchInfo, GitProvider


class PullRequestMimic:
    """
    This class mimics the PullRequest class from the PyGithub library for the LocalGitProvider.
    """

    def __init__(self, title: str, diff_files: List[FilePatchInfo]):
        self.title = title
        self.diff_files = diff_files


class LocalGitProvider(GitProvider):
    """
    This class implements the GitProvider interface for local git repositories.
    It mimics the PR functionality of the GitProvider interface,
    but does not require a hosted git repository.
    Instead of providing a PR url, the user provides a local branch path to generate a diff-patch.
    For the MVP it only supports the /review and /describe capabilities.
    """

    def __init__(self, branch_name, incremental=False):
        self.repo_path = _find_repository_root()
        self.repo = Repo(self.repo_path)
        self.head_branch_name = self.repo.head.ref.name
        self.branch_name = branch_name
        self.tmp_branch_name = f'pr_agent_{uuid.uuid4()}'
        self._prepare_repo()
        self.diff_files = None
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())
        self.description_path = settings.get('local.description_path') \
            if settings.get('local.description_path') is not None else self.repo_path / 'description.md'
        self.review_path = settings.get('local.review_path') \
            if settings.get('local.review_path') is not None else self.repo_path / 'review.md'
        # inline code comments are not supported for local git repositories
        settings.pr_reviewer.inline_code_comments = False

    def __del__(self):
        logging.debug('Deleting temporary branch...')
        self.repo.git.checkout(self.head_branch_name)  # switch back to the original branch
        # delete the temporary branch
        if self.tmp_branch_name not in self.repo.heads:
            return
        try:
            self.repo.delete_head(self.tmp_branch_name, force=True)
        except GitCommandError as e:
            raise ValueError(
                'Error while trying to delete the temporary branch.'
                'Ensure the branch exists.'
            ) from e

    def prepare_repo(self):
        """
        Prepare the repository for PR-mimic generation.
        """
        logging.debug('Preparing repository for PR-mimic generation...')
        if self.repo.is_dirty():
            raise ValueError('The repository is not in a clean state. Please check in all files.')
        if self.tmp_branch_name in self.repo.heads:
            self.repo.delete_head(self.tmp_branch_name, force=True)
        self.repo.git.checkout('HEAD', b=self.tmp_branch_name)

        try:
            logging.debug('Rebasing the temporary branch on the main branch...')
            self.repo.git.rebase('main')
        except GitCommandError as e:
            raise ValueError('Error while rebasing. Resolve conflicts before retrying.') from e

    def is_supported(self, capability: str) -> bool:
        # TODO implement
        pass

    def get_diff_files(self) -> list[FilePatchInfo]:
        diffs = self.repo.head.commit.diff(
            self.repo.branches[self.branch_name].commit,
            create_patch=True,
            R=True
        )
        diff_files = []
        for diff_item in diffs:
            if diff_item.a_blob is not None:
                original_file_content_str = diff_item.a_blob.data_stream.read().decode('utf-8')
            else:
                original_file_content_str = ""  # empty file
            if diff_item.b_blob is not None:
                new_file_content_str = diff_item.b_blob.data_stream.read().decode('utf-8')
            else:
                new_file_content_str = ""  # empty file
            edit_type = EDIT_TYPE.MODIFIED
            if diff_item.new_file:
                edit_type = EDIT_TYPE.ADDED
            elif diff_item.deleted_file:
                edit_type = EDIT_TYPE.DELETED
            elif diff_item.renamed_file:
                edit_type = EDIT_TYPE.RENAMED
            diff_files.append(
                FilePatchInfo(original_file_content_str,
                              new_file_content_str,
                              diff_item.diff.decode('utf-8'),
                              diff_item.b_path,
                              edit_type=edit_type,
                              old_filename=None if diff_item.a_path == diff_item.b_path else diff_item.a_path
                              )
            )
        self.diff_files = diff_files
        return diff_files

    def get_files(self) -> List[str]:
        """
        Returns a list of files with changes in the diff.
        """
        # Assert existence of specific branch
        branch_names = [ref.name for ref in self.repo.branches]
        if self.branch_name not in branch_names:
            raise KeyError(f"Branch: {self.branch_name} does not exist")
        branch = self.repo.branches[self.branch_name]
        # Compare the two branches
        diff_index = self.repo.head.commit.diff(branch.commit)
        # Get the list of changed files
        diff_files = [item.a_path for item in diff_index]
        return diff_files

    def publish_description(self, pr_title: str, pr_body: str):
        with open(self.description_path, "w") as file:
            # Write the string to the file
            file.write(pr_title + '\n' + pr_body)

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        with open(self.review_path, "w") as file:
            # Write the string to the file
            file.write(pr_comment)

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str):
        raise NotImplementedError('Publishing inline comments is not implemented for the local git provider')

    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str):
        raise NotImplementedError('Creating inline comments is not implemented for the local git provider')

    def publish_inline_comments(self, comments: list[dict]):
        raise NotImplementedError('Publishing inline comments is not implemented for the local git provider')

    def publish_code_suggestion(self, body: str, relevant_file: str,
                                relevant_lines_start: int, relevant_lines_end: int):
        raise NotImplementedError('Publishing code suggestions is not implemented for the local git provider')

    def publish_code_suggestions(self, code_suggestions: list):
        raise NotImplementedError('Publishing code suggestions is not implemented for the local git provider')

    def publish_labels(self, labels):
        pass  # Not applicable to the local git provider, but required by the interface

    def remove_initial_comment(self):
        pass  # Not applicable to the local git provider, but required by the interface

    def get_languages(self):
        """
        Calculate percentage of languages in repository. Used for hunk prioritisation.
        """
        # Get all files in repository
        filepaths = [Path(item.path) for item in self.repo.tree().traverse() if item.type == 'blob']
        # Identify language by file extension and count
        lang_count = Counter(ext.lstrip('.') for filepath in filepaths for ext in [filepath.suffix.lower()])
        # Convert counts to percentages
        total_files = len(filepaths)
        lang_percentage = {lang: count / total_files * 100 for lang, count in lang_count.items()}
        return lang_percentage

    def get_pr_branch(self):
        return self.repo.head

    def get_user_id(self):
        return -1  # Not used anywhere for the local provider, but required by the interface

    def get_pr_description(self):
        commits_diff = list(self.repo.iter_commits(self.branch_name + '..HEAD'))
        # Get the commit messages and concatenate
        commit_messages = " ".join([commit.message for commit in commits_diff])
        # TODO Handle the description better - maybe use gpt-3.5 summarisation here?
        return commit_messages[:200]  # Use max 200 characters

    def get_pr_title(self):
        """
        Substitutes the branch-name as the PR-mimic title.
        """
        return self.branch_name

    def get_issue_comments(self):
        raise NotImplementedError('Getting issue comments is not implemented for the local git provider')

    def get_labels(self):
        raise NotImplementedError('Getting labels is not implemented for the local git provider')