from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from openhands.core.config.condenser_config import SubtaskAwareCondenserConfig
from openhands.core.logger import openhands_logger as logger
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


class WorkflowStateSummary(BaseModel):
    """Structured representation of workflow progress and state.

    This model captures the current status of the 8-stage problem-solving workflow
    and determines whether compression is needed based on task state transitions.
    """

    # Compression decision
    should_compress: bool = Field(
        description='Whether compression is needed. True if any stage moved to DONE, False otherwise.'
    )

    # Task tracker - status of each workflow stage
    reading_status: str = Field(
        default='TODO',
        description='Status of READING stage: TODO, IN_PROGRESS, or DONE',
    )
    running_status: str = Field(
        default='TODO',
        description='Status of RUNNING stage: TODO, IN_PROGRESS, or DONE',
    )
    exploration_status: str = Field(
        default='TODO',
        description='Status of EXPLORATION stage: TODO, IN_PROGRESS, or DONE',
    )
    test_creation_status: str = Field(
        default='TODO',
        description='Status of TEST_CREATION stage: TODO, IN_PROGRESS, or DONE',
    )
    fix_analysis_status: str = Field(
        default='TODO',
        description='Status of FIX_ANALYSIS stage: TODO, IN_PROGRESS, or DONE',
    )
    fix_implementation_status: str = Field(
        default='TODO',
        description='Status of FIX_IMPLEMENTATION stage: TODO, IN_PROGRESS, or DONE',
    )
    verification_status: str = Field(
        default='TODO',
        description='Status of VERIFICATION stage: TODO, IN_PROGRESS, or DONE',
    )
    final_review_status: str = Field(
        default='TODO',
        description='Status of FINAL_REVIEW stage: TODO, IN_PROGRESS, or DONE',
    )

    # Summary fields (only populated when should_compress is True)
    user_context: str = Field(
        default='',
        description='One-line description of the PR/issue and user goals',
    )
    workflow_stage: str = Field(
        default='',
        description='Current workflow stage based on task tracker',
    )
    completed_stages: str = Field(
        default='',
        description='List of completed stages with brief results',
    )
    pending_stages: str = Field(
        default='',
        description='List of pending stages',
    )
    current_state: str = Field(
        default='',
        description='Key variables, file locations, or context needed for next steps',
    )
    code_changes: str = Field(
        default='',
        description='Files modified, function signatures, and data structures changed',
    )
    test_status: str = Field(
        default='',
        description='Test status: failing test cases, error messages, expected vs actual outputs',
    )

    @classmethod
    def tool_description(cls) -> dict[str, Any]:
        """Tool description for LLM function calling with structured output."""
        properties = {}

        for field_name, field in cls.model_fields.items():
            description = field.description or ''
            properties[field_name] = {'type': 'string', 'description': description}

        # Override should_compress to be boolean type
        properties['should_compress'] = {'type': 'boolean', 'description': properties['should_compress']['description']}

        return {
            'type': 'function',
            'function': {
                'name': 'create_workflow_summary',
                'description': 'Creates a structured summary of workflow progress. Set should_compress to True only if any workflow stage transitioned to DONE.',
                'parameters': {
                    'type': 'object',
                    'properties': properties,
                    'required': [
                        'should_compress',
                        'reading_status',
                        'running_status',
                        'exploration_status',
                        'test_creation_status',
                        'fix_analysis_status',
                        'fix_implementation_status',
                        'verification_status',
                        'final_review_status',
                    ],
                },
            },
        }


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
        keep_first: int = 2,  # System prompt + instruction
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

        Uses structured function calling to return workflow state and compression decision.

        Args:
            view: The current view of events

        Returns:
            dict with keys:
                - 'should_compress': bool
                - 'task_tracker': dict with status of each stage
                - 'summary': the summary text (if compression needed)
        """
        if not self.subtask_detection_enabled:
            return {
                'should_compress': False,
                'reason': 'subtask_detection_disabled',
                'task_tracker': {},
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

        # Build conversation text from events
        conversation_text = ''
        for event in events_to_analyze:
            event_content = self._truncate(str(event))
            conversation_text += f'<EVENT id={event.id}>\n{event_content}\n</EVENT>\n\n'

        # Get previous summary
        previous_summary = summary_event.message if summary_event.message else ''

        # Construct the prompt
        prompt = f"""You are maintaining a context-aware state summary for a software debugging agent.
You will be given a list of events corresponding to actions taken by the agent, and the most recent previous summary if one exists.

## PROBLEM-SOLVING WORKFLOW (8 Stages)

Each stage represents a major milestone in solving the problem:

1. **READING** - Understand and reword the problem in clearer terms
2. **RUNNING** - Install and run tests to understand the environment
3. **EXPLORATION** - Find files related to the problem and identify solutions
4. **TEST CREATION** - Create a script to reproduce and verify the issue
5. **FIX ANALYSIS** - Analyze and state clearly how to fix the problem
6. **FIX IMPLEMENTATION** - Edit source code to implement the solution
7. **VERIFICATION** - Test implementation thoroughly to verify the fix works
8. **FINAL REVIEW** - Carefully re-read requirements and compare with base commit

## COMPRESSION DECISION (CRITICAL)

Track which workflow stages have been COMPLETED:
- When a stage progresses from TODO → IN_PROGRESS → DONE, that's a MAJOR MILESTONE
- If ANY stage newly transitioned to DONE → set should_compress to True
- If NO stages changed status or only moved from TODO → IN_PROGRESS → set should_compress to False

## TASK TRACKER FORMAT (Current Status of Each Workflow Stage)

For each stage, determine its current status: TODO, IN_PROGRESS, or DONE

## PREVIOUS SUMMARY (if exists)
{previous_summary}

## EVENTS TO ANALYZE
{conversation_text}

## DECISION PROCESS

1. Review the new events and update the status of each workflow stage
2. Compare with the previous summary's task tracker
3. Identify which stages transitioned to DONE
4. Set should_compress to True if any stage moved to DONE, False otherwise
5. If should_compress is True, populate the summary fields with completed_stages, pending_stages, etc.

Do not explain your decision. Follow the decision process exactly.
"""

        messages = [Message(role='user', content=[TextContent(text=prompt)])]

        try:
            # Use function calling with structured output
            response = self.llm.completion(
                messages=self.llm.format_messages_for_llm(messages),
                tools=[WorkflowStateSummary.tool_description()],
                tool_choice={
                    'type': 'function',
                    'function': {'name': 'create_workflow_summary'},
                },
            )

            # Extract the message containing tool calls
            message = response.choices[0].message

            # Check if there are tool calls
            if not hasattr(message, 'tool_calls') or not message.tool_calls:
                raise ValueError('No tool calls found in response')

            # Find the create_workflow_summary tool call
            summary_tool_call = None
            for tool_call in message.tool_calls:
                if tool_call.function.name == 'create_workflow_summary':
                    summary_tool_call = tool_call
                    break

            if not summary_tool_call:
                raise ValueError('create_workflow_summary tool call not found')

            # Parse the arguments
            args_json = summary_tool_call.function.arguments
            args_dict = json.loads(args_json)

            # Create a WorkflowStateSummary object
            summary = WorkflowStateSummary.model_validate(args_dict)

            # Record metadata
            self.add_metadata('response', response.model_dump())
            self.add_metadata('metrics', self.llm.metrics.get())

            # Build task tracker dict from the summary
            task_tracker = {
                'READING': summary.reading_status,
                'RUNNING': summary.running_status,
                'EXPLORATION': summary.exploration_status,
                'TEST_CREATION': summary.test_creation_status,
                'FIX_ANALYSIS': summary.fix_analysis_status,
                'FIX_IMPLEMENTATION': summary.fix_implementation_status,
                'VERIFICATION': summary.verification_status,
                'FINAL_REVIEW': summary.final_review_status,
            }

            return {
                'should_compress': summary.should_compress,
                'task_tracker': task_tracker,
                'summary': self._format_summary(summary) if summary.should_compress else '',
            }

        except (ValueError, AttributeError, KeyError, json.JSONDecodeError) as e:
            logger.warning(f'Failed to parse summary tool call: {e}. No compression.')
            self.add_metadata('llm_error', str(e))
            return {
                'should_compress': False,
                'reason': f'llm_error: {str(e)}',
                'task_tracker': {},
                'summary': ''
            }

    def _format_summary(self, summary: WorkflowStateSummary) -> str:
        """Format the structured summary into readable text."""
        sections = [
            '# Workflow Progress Summary',
            '',
            f'**User Context**: {summary.user_context}',
            f'**Current Stage**: {summary.workflow_stage}',
            '',
            '## Completed Stages',
            summary.completed_stages if summary.completed_stages else 'None',
            '',
            '## Pending Stages',
            summary.pending_stages if summary.pending_stages else 'None',
            '',
            '## Current State',
            summary.current_state if summary.current_state else 'No state recorded',
            '',
            '## Code Changes',
            summary.code_changes if summary.code_changes else 'No changes recorded',
            '',
            '## Test Status',
            summary.test_status if summary.test_status else 'No test status recorded',
            '',
            '## Task Tracker',
            f'READING: {summary.reading_status}',
            f'RUNNING: {summary.running_status}',
            f'EXPLORATION: {summary.exploration_status}',
            f'TEST_CREATION: {summary.test_creation_status}',
            f'FIX_ANALYSIS: {summary.fix_analysis_status}',
            f'FIX_IMPLEMENTATION: {summary.fix_implementation_status}',
            f'VERIFICATION: {summary.verification_status}',
            f'FINAL_REVIEW: {summary.final_review_status}',
        ]

        return '\n'.join(sections)

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
            self.add_metadata('task_tracker', result.get('task_tracker', {}))
            return True

        # Use LLM to check and potentially generate summary
        result = self._check_and_summarize_with_llm(view)

        if result['should_compress']:
            # Cache the summary for get_condensation()
            self._cached_summary = result['summary']
            self._cached_decision = 'llm_detected_workflow_completion'
            self.add_metadata('trigger_reason', 'workflow_stage_completed')
            self.add_metadata('task_tracker', result.get('task_tracker', {}))
            return True
        else:
            # No compression needed
            self._cached_summary = None
            self._cached_decision = None
            self.add_metadata('no_compression_reason', result.get('reason', 'no_stage_completed'))
            self.add_metadata('task_tracker', result.get('task_tracker', {}))
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
