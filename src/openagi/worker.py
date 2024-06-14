import logging
from pathlib import Path
import re
from textwrap import dedent
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from openagi.actions.utils import run_action
from openagi.exception import OpenAGIException
from openagi.llms.base import LLMBaseModel
from openagi.memory.memory import Memory
from openagi.prompts.worker_task_execution import WorkerAgentTaskExecution
from openagi.tasks.task import Task
from openagi.utils.extraction import get_act_classes_from_json, get_last_json
from openagi.utils.helper import get_default_id


class Worker(BaseModel):
    id: str = Field(default_factory=get_default_id)
    role: str = Field(description="Role of the worker.")
    instructions: Optional[str] = Field(description="Instructions the worker should follow.")
    llm: Optional[LLMBaseModel] = Field(
        description="LLM Model to be used.",
        default=None,
        exclude=True,
    )
    memory: Optional[Memory] = Field(
        default_factory=list,
        description="Memory to be used.",
        exclude=True,
    )
    actions: Optional[List[Any]] = Field(
        description="Actions that the Worker supports",
        default_factory=list,
    )
    max_iterations: int = Field(
        default=20,
        description="Maximum number of steps to achieve the objective.",
    )
    output_key: str = Field(
        default="final_output",
        description="Key to be used to store the output.",
    )
    force_output: bool = Field(
        default=True,
        description="If set to True, the output will be overwritten even if it exists.",
    )

    # Validate output_key. Should contain only alphabets and only underscore are allowed. Not alphanumeric
    @field_validator("output_key")
    @classmethod
    def validate_output_key(cls, v, values, **kwargs):
        if not re.match("^[a-zA-Z_]+$", v):
            raise ValueError(
                f"Output key should contain only alphabets and only underscore are allowed. Got {v}"
            )
        return v

    class Config:
        arbitrary_types_allowed = True

    def worker_doc(self):
        """Returns a dictionary containing information about the worker, including its ID, role, description, and the supported actions."""
        return {
            "worker_id": self.id,
            "role": self.role,
            "description": self.instructions,
            "supported_actions": [action.cls_doc() for action in self.actions],
        }

    def provoke_thought_obs(self, observation):
        thoughts = dedent(f"""Observation: {observation}""".strip())
        return thoughts

    def should_continue(self, llm_resp: str) -> Union[bool, Optional[Dict]]:
        output: Dict = get_last_json(llm_resp, llm=self.llm, max_iterations=self.max_iterations)
        output_key_exists = bool(output and output.get(self.output_key))
        return (not output_key_exists, output)

    def _force_output(
        self, llm_resp: str, all_thoughts_and_obs: List[str]
    ) -> Union[bool, Optional[str]]:
        """Force the output once the max iterations are reached."""
        prompt = (
            "\n".join(all_thoughts_and_obs)
            + "Based on the previous action and observation, give me the output."
        )
        output = self.llm.run(prompt)
        cont, final_output = self.should_continue(output)
        if cont:
            prompt = (
                "\n".join(all_thoughts_and_obs)
                + f"Based on the previous action and observation, give me the output. {final_output}"
            )
            output = self.llm.run(prompt)
            cont, final_output = self.should_continue(output)
        if cont:
            raise OpenAGIException(
                f"LLM did not produce the expected output after {self.max_iterations} iterations."
            )
        return (cont, final_output)

    def save_to_memory(self, task: Task):
        """Saves the output to the memory."""
        return self.memory.update_task(task)

    def execute_task(self, task: Task, context: Any = None) -> Any:
        """Executes the specified task."""
        iteration = 1
        pth = Path(f"{self.memory.session_id}/logs/{task.name}-{iteration}.log")
        pth.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=pth, level=logging.INFO, format='%(asctime)s %(message)s')
        
        logging.info(
            f"{'>'*20} Executing Task - {task.name}[{task.id}] with worker - {self.role}[{self.id}] {'<'*20}"
        )
        with open(pth, "a") as f:
            f.write(f"{'>'*20} Executing Task - {task.name}[{task.id}] with worker - {self.role}[{self.id}] {'<'*20}\n")
        task_to_execute = f"{task.description}"
        worker_description = f"{self.role} - {self.instructions}"
        all_thoughts_and_obs = []

        initial_thought_provokes = self.provoke_thought_obs(None)
        te_vars = dict(
            task_to_execute=task_to_execute,
            worker_description=worker_description,
            supported_actions=[action.cls_doc() for action in self.actions],
            thought_provokes=initial_thought_provokes,
            output_key=self.output_key,
            context=context,
            max_iterations=self.max_iterations,
        )

        base_prompt = WorkerAgentTaskExecution().from_template(te_vars)
        prompt = f"{base_prompt}\nThought:\nIteration: {iteration}\nActions:\n"

        logging.info(f"Initial prompt: {prompt}")
        with open(pth, "a") as f:
            f.write(f"\n{'*'*20}Initial prompt{'*'*20}\n{prompt}\n{'*'*20}End Initial prompt{'*'*20}\n")
        observations = self.llm.run(prompt)
        all_thoughts_and_obs.append(prompt)

        max_iters = self.max_iterations + 1
        while iteration < max_iters:
            logging.info(f"---- Iteration {iteration} ----")
            with open(pth, "a") as f:
                f.write(f"{'*'*20}---- Iteration {iteration} ----{'*'*20}\n")
            continue_flag, output = self.should_continue(observations)
            logging.info(f"Continue flag: {continue_flag}, Output: {output}")

            action = output.get("action") if output else None
            if action:
                action = [action]

            # Save to memory
            if output:
                task.result = observations
                task.actions = str([action.cls_doc() for action in self.actions])
                self.save_to_memory(task=task)
                logging.info(f"Task result saved to memory with observations: {observations}")
                with open(pth, "a") as f:
                    f.write(f"{'*'*20}----Task result saved to memory with observations----{'*'*20}\n{observations}\n{'*'*20}----End Task result saved to memory with observations----{'*'*20}\n")
            if not continue_flag:
                logging.info(f"Stopping as continue_flag is {continue_flag}. Final output: {output}")
                with open(pth, "a") as f:
                    f.write(f"{'*'*20}----Stopping as continue_flag-----{'*'*20}\nStopping as continue_flag is {continue_flag}. Final output: {output}\n{'*'*20}----End Stopping as continue_flag-----{'*'*20}\n")
                break

            if not action:
                logging.info(f"No action found in the output: {output}")
                with open(pth, "a") as f:
                    f.write(f"{'*'*20}-----No action found in the output-----{'*'*20}\n{output}\n{'*'*20}-----End No action found in the output-----{'*'*20}")
                observations = f"Action: {action}\n{observations} Unable to extract action. Verify the output and try again."
                all_thoughts_and_obs.append(observations)
                continue

            if action:
                action_json = f"```json\n{output}\n```\n"
                try:
                    actions = get_act_classes_from_json(action)
                except KeyError as e:
                    if "cls" in e or "module" in e or "kls" in e:
                        observations = f"Action: {action_json}\n{observations}"
                        all_thoughts_and_obs.append(action_json)
                        all_thoughts_and_obs.append(observations)
                        logging.error(f"KeyError during action extraction: {e}")
                        with open(pth, "a") as f:
                            f.write(f"{'*'*20}----KeyError during action extraction-----{'*'*20}\n{e}\n{'*'*20}----End KeyError during action extraction-----{'*'*20}\n")
                        continue
                    else:
                        raise e

                for act_cls, params in actions:
                    params["memory"] = self.memory
                    params["llm"] = self.llm
                    try:
                        res = run_action(action_cls=act_cls, **params)
                        logging.info(f"Action {act_cls} executed with result: {res}")
                        with open(pth, "a") as f:
                            f.write(f"{'*'*20}----Action {act_cls} executed with result----{'*'*20}\nAction {act_cls} executed with result: {res}\n{'*'*20}----End Action {act_cls} executed with result----{'*'*20}\n")
                    except Exception as e:
                        logging.error(f"Error during action execution: {e}")
                        with open(pth, "a") as f:
                            f.write(f"{'*'*20}----Error during action execution----{'*'*20}\nError during action execution: {e}\n{'*'*20}----End Error during action execution----{'*'*20}\n")
                        observations = f"Action: {action_json}\n{observations}. {e} Try to fix the error and try again. Ignore if already tried more than twice"
                        all_thoughts_and_obs.append(action_json)
                        all_thoughts_and_obs.append(observations)
                        continue

                    observation_prompt = f"Observation: {res}\n"
                    all_thoughts_and_obs.append(action_json)
                    all_thoughts_and_obs.append(observation_prompt)
                    observations = res

                thought_prompt = self.provoke_thought_obs(observations)
                all_thoughts_and_obs.append(f"\n{thought_prompt}\nActions:\n")

                prompt = f"{base_prompt}\n" + "\n".join(all_thoughts_and_obs)
                logging.debug(f"\nSTART:{'*' * 20}\n{prompt}\n{'*' * 20}:END")
                with open(pth, "a") as f:
                    f.write(f"\n{'*' * 20}-----Prompt-----{'*' * 20}\nSTART:\n{prompt}\n:END\n{'*' * 20}-----End Prompt-----{'*' * 20}\n")
                with open(pth, "a") as f:
                    f.write(f"{prompt}\n")
                logging.info(f"Log for iteration {iteration} updated to {pth}")
                with open(pth, 'a') as f:
                    f.write(f"\n{'*'*20}----Log for iteration----{'*'*20}\nLog for iteration {iteration} updated to {pth}\n{'*'*20}----End Log for iteration----{'*'*20}\n")
                observations = self.llm.run(prompt)
            iteration += 1
        else:
            if iteration == self.max_iterations:
                logging.info("---- Forcing Output ----")
                with open(pth, "a") as f:
                    f.write("\n---- Forcing Output ----\n")
                if self.force_output:
                    cont, final_output = self._force_output(observations, all_thoughts_and_obs)
                    if cont:
                        raise OpenAGIException(
                            f"LLM did not produce the expected output after {iteration} iterations for task {task.name}"
                        )
                    output = final_output
                    task.result = observations
                    task.actions = str([action.cls_doc() for action in self.actions])
                    self.save_to_memory(task=task)
                    logging.info(f"Forced output saved to memory with observations: {observations}")
                    with open(pth, "a") as f:
                        f.write(f"\n{'*'*20}----Forced output saved to memory with observations----{'*'*20}\nForced output saved to memory with observations: {observations}\n{'*'*20}----End Forced output saved to memory with observations----{'*'*20}\n")
                else:
                    raise OpenAGIException(
                        f"LLM did not produce the expected output after {iteration} iterations for task {task.name}"
                    )

        logging.info(
            f"Task Execution Completed - {task.name} with worker - {self.role}[{self.id}] in {iteration} iterations"
        )
        with open(pth, "a") as f:
            f.write(f"\n{'*'*20}-----Task Execution Completed - {task.name} with worker - {self.role}[{self.id}] in {iteration} iterations-----{'*'*20}\n")
        return output, task
