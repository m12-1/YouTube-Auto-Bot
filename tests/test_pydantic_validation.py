"""
tests/test_pydantic_validation.py

Tests for the Pydantic module_contract decorator and base models.
"""

import unittest
from typing import Dict, Any
from pydantic import Field
from shared.json_contract import BaseModuleInput, BaseModuleOutput, module_contract

class DummyInput(BaseModuleInput):
    value: int = Field(ge=0)

class DummyOutput(BaseModuleOutput):
    data: Dict[str, Any]

@module_contract(DummyInput, DummyOutput, "dummy_module")
def dummy_run(input_data: DummyInput) -> DummyOutput:
    return DummyOutput(
        success=True,
        error=None,
        message="Success",
        data={"result": input_data.value * 2},
        execution_time=0.0,
        stage="dummy_module",
        status="success",
        module="dummy_module"
    )

class TestPydanticValidation(unittest.TestCase):

    def test_valid_input(self):
        input_json = {"run_id": "test_123", "value": 10}
        response = dummy_run(input_json)
        
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["success"], True)
        self.assertEqual(response["data"]["result"], 20)
        self.assertEqual(response["module"], "dummy_module")
        self.assertIn("execution_time", response)

    def test_invalid_input(self):
        input_json = {"run_id": "test_123", "value": -5} # Fails ge=0 validation
        response = dummy_run(input_json)
        
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["success"], False)
        self.assertTrue("Validation failed" in response["error"])

    def test_missing_run_id(self):
        input_json = {"value": 10} # Missing run_id
        response = dummy_run(input_json)
        
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["success"], False)
        self.assertTrue("Validation failed" in response["error"])

if __name__ == "__main__":
    unittest.main()
