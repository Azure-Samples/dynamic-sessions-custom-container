#!/usr/bin/env python3
"""
Azure Container Apps Dynamic Sessions - Session Server
Provides HTTP endpoints for secure code execution within isolated sessions.
"""

import json
import subprocess
import sys
import tempfile
import os
from flask import Flask, request, jsonify
import traceback

app = Flask(__name__)


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "message": "Dynamic session container is ready"
    })

@app.route('/test', methods=['GET', 'POST'])
def test_endpoint():
    """Test endpoint to verify connectivity"""
    return jsonify({
        "status": "healthy",
        "message": "Session container is ready",
        "properties": {
            "status": "Success", 
            "stdout": "Test endpoint working",
            "stderr": "",
            "returnCode": 0
        }
    })

@app.route('/execute', methods=['POST', 'GET'])
def execute_code():
    """Execute code in the session container"""
    if request.method == 'GET':
        return jsonify({"message": "Execute endpoint is working", "method": "GET"})
        
    try:
        # Parse the request
        data = None
        try:
            data = request.get_json(force=True)  # Force parsing even if content-type is wrong
        except Exception as json_error:
            print(f"❌ JSON parsing failed: {json_error}", flush=True)
            return jsonify({"error": f"JSON parsing failed: {str(json_error)}"}), 400
        
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        # Extract properties from the request (support both nested and top-level shapes)
        properties = data.get('properties', {})
        if not properties:
            # Fallback: build properties from top-level fields if present
            tl_code = data.get('code')
            tl_cmd = data.get('shellCommand') or data.get('command')
            tl_lang = data.get('language')
            tl_timeout = data.get('timeout') or data.get('timeoutInSeconds')
            properties = {
                **properties,
                **({ 'code': tl_code } if tl_code is not None else {}),
                **({ 'shellCommand': tl_cmd } if tl_cmd is not None else {}),
                **({ 'language': tl_lang } if tl_lang is not None else {}),
                **({ 'timeoutInSeconds': tl_timeout } if tl_timeout is not None else {}),
            }
        
        code = properties.get('code', '')
        shell_command = properties.get('shellCommand', '')
        language = properties.get('language', 'python')
        timeout = properties.get('timeoutInSeconds', 30)
        
        # Check if we have actual content (not just empty/whitespace)
        has_code = bool(code and code.strip())
        has_shell_command = bool(shell_command and shell_command.strip())
        
        # Validation - ensure we have something to execute
        if not has_code and not has_shell_command:
            return jsonify({"error": "No code or command provided"}), 400
        
        # Execute based on type
        if shell_command:
            # Execute shell command
            result = subprocess.run(
                shell_command, 
                shell=True, 
                capture_output=True, 
                text=True, 
                timeout=timeout
            )
            stdout = result.stdout
            stderr = result.stderr
            return_code = result.returncode
            
            return jsonify({
                "properties": {
                    "status": "Success" if return_code == 0 else "Failed",
                    "stdout": stdout,
                    "stderr": stderr,
                    "returnCode": return_code,
                    "executionTimeInMilliseconds": 0  # Simplified
                }
            })
        
        elif code:
            # Execute code based on language
            if language.lower() == 'python':
                # Execute Python code
                result = subprocess.run(
                    [sys.executable, '-c', code],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd='/workspace'
                )
            elif language.lower() in ['javascript', 'js']:
                # Execute JavaScript code
                with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
                    f.write(code)
                    temp_file = f.name
                
                try:
                    result = subprocess.run(
                        ['node', temp_file],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd='/workspace'
                    )
                finally:
                    os.unlink(temp_file)
            
            elif language.lower() in ['bash', 'sh']:
                # Execute bash code
                result = subprocess.run(
                    ['bash', '-c', code],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd='/workspace'
                )
            
            elif language.lower() in ['powershell', 'pwsh']:
                # Execute PowerShell code
                result = subprocess.run(
                    ['pwsh', '-c', code],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd='/workspace'
                )
            
            else:
                return jsonify({"error": f"Unsupported language: {language}"}), 400
            
            stdout = result.stdout
            stderr = result.stderr
            return_code = result.returncode
            
            return jsonify({
                "properties": {
                    "status": "Success" if return_code == 0 else "Failed",
                    "stdout": stdout,
                    "stderr": stderr,
                    "returnCode": return_code,
                    "executionResult": stdout if return_code == 0 else stderr,
                    "executionTimeInMilliseconds": 0  # Simplified
                }
            })
        
    except subprocess.TimeoutExpired:
        return jsonify({
            "properties": {
                "status": "Failed",
                "stderr": "Execution timed out",
                "executionTimeInMilliseconds": timeout * 1000
            }
        }), 408
        
    except Exception as e:
        return jsonify({
            "properties": {
                "status": "Failed", 
                "stderr": f"Execution error: {str(e)}\n{traceback.format_exc()}",
                "executionTimeInMilliseconds": 0
            }
        }), 500

@app.route('/', methods=['GET', 'POST'])
def root():
    """Root endpoint - redirect to execute for POST, info for GET"""
    if request.method == 'POST':
        return execute_code()
    return jsonify({
        "message": "Azure Container Apps Dynamic Session Container",
        "status": "healthy",
        "endpoints": {
            "POST /": "Execute code (redirects to /execute)",
            "POST /execute": "Execute code",
            "GET /health": "Health check"
        }
    })

if __name__ == '__main__':
    # For local development only - production uses gunicorn
    print("🚀 Starting Azure Container Apps Dynamic Session server...", flush=True)
    print("📡 Listening on port 8080", flush=True)
    
    # Startup validation - verify Python execution works
    try:
        test_result = subprocess.run(
            [sys.executable, '-c', 'print("startup_ok")'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if test_result.returncode == 0 and 'startup_ok' in test_result.stdout:
            print("✅ Python execution validated", flush=True)
        else:
            print(f"⚠️ Python execution test failed: {test_result.stderr}", flush=True)
    except Exception as e:
        print(f"⚠️ Startup validation error: {e}", flush=True)
    
    sys.stdout.flush()
    app.run(host='0.0.0.0', port=8080, debug=False)