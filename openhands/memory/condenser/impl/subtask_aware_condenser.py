from __future__ import annotations

from openhands.core.config.condenser_config import SubtaskAwareCondenserConfig
from openhands.core.message import Message, TextContent
from openhands.events.action.agent import CondensationAction
from openhands.events.observation.agent import AgentCondensationObservation
from openhands.events.serialization.event import truncate_content
from openhands.llm.llm import LLM
from openhands.llm.llm_registry import LLMRegistry
from openhands.memory.condenser.condenser import (
    Condensation,
    RollingCondenser,
    View,
)


class SubtaskAwareCondenser(RollingCondenser):
    """A condenser that uses LLM to intelligently decide when and how to compress.

    This condenser uses a SINGLE LLM call to:
    1. Decide if compression is needed (based on workflow stage and errors)
    2. Generate a structured summary if compression is needed

    The compression decision is based on the debugging workflow:
    - EXPLORE: Finding and reading relevant files
    - REPRODUCE: Creating script to demonstrate the issue
    - FIX: Editing source code to resolve the issue
    - VERIFY: Running reproduction script with fix applied
    - VALIDATE: Testing edge cases and robustness
    """

    def __init__(
        self,
        llm: LLM,
        max_size: int = 100,
        keep_first: int = 4,  # System prompt + instruction
        max_event_length: int = 10_000,
        subtask_detection_enabled: bool = True,
    ):
        if keep_first < 1:
            raise ValueError(f'keep_first ({keep_first}) must be at least 1')
        if max_size < keep_first + 2:
            raise ValueError(
                f'max_size ({max_size}) must be at least keep_first + 2 ({keep_first + 2})'
            )

        self.max_size = max_size
        self.keep_first = keep_first
        self.max_event_length = max_event_length
        self.llm = llm
        self.subtask_detection_enabled = subtask_detection_enabled

        # Cache for the LLM response
        self._cached_summary = None
        self._cached_decision = None

        super().__init__()

    def _truncate(self, content: str) -> str:
        """Truncate the content to fit within the specified maximum event length."""
        return truncate_content(content, max_chars=self.max_event_length)

    def _check_and_summarize_with_llm(self, view: View) -> dict:
        """Use LLM to decide if compression is needed AND generate summary in one call.

        This follows the exact prompt structure you provided, where the LLM:
        1. First checks if compression is needed
        2. If NO: outputs "NO_COMPRESSION_NEEDED"
        3. If YES: outputs a structured summary

        Args:
            view: The current view of events

        Returns:
            dict with keys:
                - 'decision': 'COMPRESSION_NEEDED' or 'NO_COMPRESSION_NEEDED'
                - 'summary': the summary text (if compression needed)
        """
        if not self.subtask_detection_enabled:
            return {
                'decision': 'NO_COMPRESSION_NEEDED',
                'reason': 'subtask_detection_disabled',
                'summary': ''
            }

        # Check if there's an existing summary event
        summary_event_idx = self.keep_first
        if summary_event_idx < len(view) and isinstance(
            view[summary_event_idx], AgentCondensationObservation
        ):
            summary_event = view[summary_event_idx]
            start_idx = summary_event_idx + 1
        else:
            summary_event = AgentCondensationObservation('No events summarized yet')
            start_idx = self.keep_first

        # Identify events to analyze (everything after head/summary)
        events_to_analyze = []
        for event in view[start_idx:]:
            if not isinstance(event, AgentCondensationObservation):
                events_to_analyze.append(event)

        if len(events_to_analyze) < 3:
            return {
                'decision': 'NO_COMPRESSION_NEEDED',
                'reason': 'insufficient_events',
                'summary': ''
            }

        # Build conversation text from events
        conversation_text = ''
        for event in events_to_analyze:
            event_content = self._truncate(str(event))
            conversation_text += f'<EVENT id={event.id}>\n{event_content}\n</EVENT>\n\n'

        # Get previous summary
        previous_summary = summary_event.message if summary_event.message else 'None'

        # Construct the COMBINED prompt (decision + summarization)
        # This is based on your provided prompt structure
        combined_prompt = f"""You are maintaining a context-aware state summary for a software debugging agent.

## COMPRESSION DECISION (Check FIRST)

Examine the recent conversation for <returncode> in USER messages:
- If ANY recent command has returncode != 0: Output "NO_COMPRESSION_NEEDED"
- If agent is still in exploration phase (steps 1-2): Output "NO_COMPRESSION_NEEDED"
- If a clear subtask milestone is reached (steps 3-5): Proceed to compress

{conversation_text}

## DEBUGGING WORKFLOW STAGES

1. **EXPLORE** - Find and read relevant files
2. **REPRODUCE** - Create script demonstrating the issue
3. **FIX** - Edit source code to resolve the issue
4. **VERIFY** - Run reproduction script with fix applied
5. **VALIDATE** - Test edge cases and robustness

## STATE TRACKING FORMAT

When compression IS needed, use this structure:

USER_CONTEXT: [One-line description of the PR/issue and user goals]

WORKFLOW_STAGE: [EXPLORE|REPRODUCE|FIX|VERIFY|VALIDATE]

COMPLETED:
- [Subtask 1 with brief result]
- [Subtask 2 with brief result]

PENDING:
- [Next immediate task]
- [Future tasks if known]

CURRENT_STATE:
- [Key variables, file locations, or context needed for next steps]

CODE_STATE:
- File paths: [list modified or relevant files]
- Function signatures: [key functions being modified]
- Data structures: [relevant data structures if any]

TESTS:
- Failing cases: [list failing test cases]
- Error messages: [any error output]
- Outputs: [expected vs actual outputs]

CHANGES:
- [File:line]: [description of code edit]
- [Variable updates or state changes]

DEPS:
- Dependencies: [external libraries used]
- Imports: [new imports added]
- External calls: [API or system calls]

VERSION_CONTROL_STATUS:
- Repository state: [clean/modified/staged]
- Current branch: [branch name if known]
- Commit history: [relevant commits if any]

## PREVIOUS SUMMARY (if exists)

{previous_summary}

## EXAMPLES

Example 1: Mid-exploration (NO compression)

Agent runs: ls -la
Agent runs: grep -R "separability"
Agent runs: cat file.py | head -50

Output: NO_COMPRESSION_NEEDED
Reason: Still exploring codebase, no subtask completed

Example 2: After successful reproduction

Agent created reproduction script showing bug
Script output confirmed incorrect behavior

Output:
USER_CONTEXT: Fix separability_matrix returning incorrect results for nested CompoundModels in astropy
WORKFLOW_STAGE: REPRODUCE ✓ → Moving to FIX
COMPLETED:
- Located bug in astropy/modeling/separable.py at line 245
- Created reproduction script confirming the issue
- Identified root cause in _cstack() function
PENDING:
- Apply fix to line 245 in separable.py
- Re-run reproduction script to verify fix
- Test edge cases
...

Example 3: After failed fix attempt

Agent tried: applypatch command
Return code: 127 (command not found)

Output: NO_COMPRESSION_NEEDED
Reason: Error occurred, preserve full context for retry

## COMPRESSION RULES

COMPRESS when:
- A workflow stage is completed (reproduced bug, applied fix, verified fix)
- All recent commands succeeded (returncode: 0)
- State can be summarized without losing critical details

NO_COMPRESSION_NEEDED when:
- Any recent command failed (returncode != 0)
- Agent is exploring/reading files (stage 1-2)
- Mid-task with incomplete information
- Recent error messages need to be preserved

## OUTPUT

Based on the conversation above, output either:
1. "NO_COMPRESSION_NEEDED" (if criteria met)
2. A concise state summary using the format above (if subtask completed successfully)

Focus on preserving actionable information needed for the agent to continue work efficiently."""

        # Single LLM call for both decision and summarization
        messages = [Message(role='user', content=[TextContent(text=combined_prompt)])]

        try:
            response = self.llm.completion(
                messages=self.llm.format_messages_for_llm(messages),
                extra_body={'metadata': self.llm_metadata},
            )
            output = response.choices[0].message.content.strip()

            # Record metadata
            self.add_metadata('llm_combined_response', output)
            self.add_metadata('llm_prompt_length', len(combined_prompt))
            self.add_metadata('llm_response_length', len(output))
            self.add_metadata('response', response.model_dump())
            self.add_metadata('metrics', self.llm.metrics.get())

            # Parse the output
            if 'NO_COMPRESSION_NEEDED' in output:
                # Extract reason if present
                reason = 'llm_suggested_no_compression'
                if 'Reason:' in output:
                    reason = output.split('Reason:')[1].strip().split('\n')[0]

                return {
                    'decision': 'NO_COMPRESSION_NEEDED',
                    'reason': reason,
                    'summary': ''
                }
            else:
                # The output is the summary
                return {
                    'decision': 'COMPRESSION_NEEDED',
                    'reason': 'llm_detected_subtask_completion',
                    'summary': output
                }

        except Exception as e:
            # If LLM fails, fall back to no compression
            self.add_metadata('llm_error', str(e))
            return {
                'decision': 'NO_COMPRESSION_NEEDED',
                'reason': f'llm_error: {str(e)}',
                'summary': ''
            }

    def get_condensation(self, view: View) -> Condensation:
        """Create a condensation using the cached LLM summary.

        This method is called AFTER should_condense() returns True.
        It uses the cached summary from the LLM call.
        """
        head = view[: self.keep_first]

        # Check if there's an existing summary event
        summary_event_idx = self.keep_first
        if summary_event_idx < len(view) and isinstance(
            view[summary_event_idx], AgentCondensationObservation
        ):
            start_idx = summary_event_idx + 1
        else:
            start_idx = self.keep_first

        # Identify ALL events to be forgotten
        forgotten_events = []
        for event in view[start_idx:]:
            if not isinstance(event, AgentCondensationObservation):
                forgotten_events.append(event)

        if not forgotten_events:
            # Nothing to condense (shouldn't happen, but handle gracefully)
            return Condensation(
                action=CondensationAction(
                    forgotten_events_start_id=0,
                    forgotten_events_end_id=0,
                    summary='No events to condense',
                    summary_offset=self.keep_first,
                )
            )

        # Use the cached summary from the LLM call
        summary = self._cached_summary or 'Summary not available'

        # Record metadata
        self.add_metadata('events_condensed', len(forgotten_events))
        self.add_metadata('head_kept', self.keep_first)
        self.add_metadata('tail_kept', 0)  # We don't keep tail
        self.add_metadata('summary_length', len(summary))

        return Condensation(
            action=CondensationAction(
                forgotten_events_start_id=min(event.id for event in forgotten_events),
                forgotten_events_end_id=max(event.id for event in forgotten_events),
                summary=summary,
                summary_offset=self.keep_first,
            )
        )

    def should_condense(self, view: View) -> bool:
        """Determine if condensation should happen using LLM-based decision.

        This method makes a SINGLE LLM call that both:
        1. Decides if compression is needed
        2. Generates the summary (if needed)

        The summary is cached for use by get_condensation().

        Triggers when:
        1. View size exceeds max_size (hard limit), OR
        2. LLM detects a subtask completion (if enabled)
        """
        # Always condense if we exceed max_size (safety limit)
        if len(view) > self.max_size:
            # Force compression, but still use LLM for summary
            result = self._check_and_summarize_with_llm(view)
            self._cached_summary = result['summary'] if result['summary'] else 'Max size exceeded, forced compression'
            self._cached_decision = 'max_size_exceeded'
            self.add_metadata('trigger_reason', 'max_size_exceeded')
            return True

        # Use LLM to check and potentially generate summary
        result = self._check_and_summarize_with_llm(view)

        if result['decision'] == 'COMPRESSION_NEEDED':
            # Cache the summary for get_condensation()
            self._cached_summary = result['summary']
            self._cached_decision = result['reason']
            self.add_metadata('trigger_reason', result['reason'])
            return True
        else:
            # No compression needed
            self._cached_summary = None
            self._cached_decision = None
            self.add_metadata('no_compression_reason', result['reason'])
            return False

    @classmethod
    def from_config(
        cls, config: SubtaskAwareCondenserConfig, llm_registry: LLMRegistry
    ) -> SubtaskAwareCondenser:
        """Create from config."""
        llm_config = config.llm_config.model_copy()
        llm_config.caching_prompt = False
        llm = llm_registry.get_llm('condenser', llm_config)

        return SubtaskAwareCondenser(
            llm=llm,
            max_size=config.max_size,
            keep_first=config.keep_first,
            max_event_length=config.max_event_length,
            subtask_detection_enabled=config.subtask_detection_enabled,
        )


# Register the config
SubtaskAwareCondenser.register_config(SubtaskAwareCondenserConfig)
