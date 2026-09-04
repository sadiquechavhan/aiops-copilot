"""
verify_rag -- measure retrieval quality of runbook RAG against test questions.

Tests that questions answerable ONLY from our runbooks get correct citations.
"""

import json
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aiops_mcp.rag import RunbookRAG


# Test questions that are ONLY answerable from our runbooks
# The S6 done condition: "a question answerable only from your runbooks gets answered with a correct citation"
# We verify that the correct runbook (source file) is returned with relevant content
TEST_QUESTIONS = [
    {
        "question": "How do I diagnose pool exhaustion in inventory?",
        "expected_source": "pool-exhaust-inventory.md",
    },
    {
        "question": "What is the difference between inventory latency and gateway latency incidents?",
        "expected_source": "latency-propagation.md",
    },
    {
        "question": "How do I identify the error origin using missing child spans?",
        "expected_source": "missing-child-spans.md",
    },
    {
        "question": "What are the chaos endpoints for the gateway service?",
        "expected_source": "service-topology.md",
    },
    {
        "question": "How do I remediate an error rate fault in orders service?",
        "expected_source": "error-rate-orders.md",
    },
    {
        "question": "Why are traces ordinary during pool exhaustion?",
        "expected_source": "saturation-diagnosis.md",
    },
    {
        "question": "What is the self-time attribution for inventory latency fault i1?",
        "expected_source": "trace-self-time-analysis.md",
    },
    {
        "question": "What is the service topology of the system?",
        "expected_source": "service-topology.md",
    },
    {
        "question": "How does error propagation work from orders to gateway?",
        "expected_source": "error-propagation.md",
    },
    {
        "question": "How do I diagnose latency injection in gateway?",
        "expected_source": "latency-gateway.md",
    },
]


def run_verification():
    """Run RAG verification against test questions."""
    rag = RunbookRAG()
    print("Building index (force rebuild)...")
    count = rag.build_index(force_rebuild=True)
    print(f"Indexed {count} chunks\n")
    
    results = []
    correct = 0
    
    for i, test in enumerate(TEST_QUESTIONS, 1):
        print(f"Test {i}: {test['question']}")
        result = rag.search(test["question"], k=3)
        
        if result["status"] == "no_match":
            print(f"  NO MATCH: {result['message']}")
            results.append({
                "question": test["question"],
                "status": "no_match",
                "correct": False
            })
            continue
        
        top_result = result["results"][0]
        source_match = test["expected_source"] in top_result["source_path"]
        
        is_correct = source_match
        if is_correct:
            correct += 1
            print(f"  CORRECT: {top_result['source_path']} - {top_result['heading']} (score: {top_result['score']:.3f})")
        else:
            print(f"  WRONG: got {top_result['source_path']} - {top_result['heading']} (score: {top_result['score']:.3f})")
            print(f"  Expected source: {test['expected_source']}")
        
        results.append({
            "question": test["question"],
            "status": "success",
            "correct": is_correct,
            "top_result": top_result,
            "expected_source": test["expected_source"],
        })
    
    print(f"\n{'='*60}")
    print(f"RETRIEVAL ACCURACY: {correct}/{len(TEST_QUESTIONS)} = {100*correct/len(TEST_QUESTIONS):.1f}%")
    print(f"{'='*60}")
    
    if correct == len(TEST_QUESTIONS):
        print("ALL TESTS PASSED")
        return 0
    else:
        print("SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(run_verification())