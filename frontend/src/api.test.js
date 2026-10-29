import test from 'node:test';
import assert from 'node:assert/strict';
import { apiRequest, ApiError } from './api.js';

function response({status=200,contentType='application/json',json={ok:true},text=''}={}) {
  return {status,ok:status>=200&&status<300,headers:{get:()=>contentType},json:async()=>json,text:async()=>text};
}

test('requests use the configured API base and include the session cookie', async()=>{
  let received;
  const result=await apiRequest('/api/health',{}, {apiBase:'http://localhost:8000/',fetchImpl:async(url,options)=>{received={url,options};return response()}});
  assert.deepEqual(result,{ok:true});
  assert.equal(received.url,'http://localhost:8000/api/health');
  assert.equal(received.options.credentials,'include');
});

test('HTML fallback pages produce a diagnostic instead of a JSON parse exception', async()=>{
  await assert.rejects(()=>apiRequest('/api/projects',{}, {fetchImpl:async()=>response({contentType:'text/html',text:'<!doctype html><html>'})}), error=>error instanceof ApiError&&error.status===200&&/received text\/html/.test(error.message)&&/<!doctype html/.test(error.message));
});

test('network failures identify the attempted API path and proxy target', async()=>{
  await assert.rejects(()=>apiRequest('/api/projects',{}, {fetchImpl:async()=>{throw new TypeError('Failed to fetch')}}),error=>error instanceof ApiError&&/\/api\/projects/.test(error.message)&&/127\.0\.0\.1:8000/.test(error.message)&&/Failed to fetch/.test(error.message));
});

test('JSON API errors retain HTTP status and backend detail', async()=>{
  await assert.rejects(()=>apiRequest('/api/projects',{}, {fetchImpl:async()=>response({status:503,json:{detail:'Database unavailable'}})}),error=>error instanceof ApiError&&error.status===503&&error.message==='Database unavailable');
});

test('expired sessions notify the app and expose a useful 401 error', async()=>{
  let expired=false;
  await assert.rejects(()=>apiRequest('/api/auth/session',{}, {fetchImpl:async()=>response({status:401,json:{detail:'Authentication required'}}),onUnauthorized:()=>{expired=true}}),error=>error.status===401&&error.message==='Authentication required');
  assert.equal(expired,true);
});

test('non-JSON error responses include their status', async()=>{
  await assert.rejects(()=>apiRequest('/api/missing',{}, {fetchImpl:async()=>response({status:404,contentType:'text/html',text:'<!doctype html>'})}),error=>error.status===404&&/HTTP 404/.test(error.message));
});

test('204 responses are accepted without a JSON content type', async()=>{
  const result=await apiRequest('/api/projects/cases/1',{}, {fetchImpl:async()=>response({status:204,contentType:'',json:null})});
  assert.equal(result,null);
});
