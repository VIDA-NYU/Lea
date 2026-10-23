import assert from 'node:assert/strict';
import test from 'node:test';
import {serviceEnvironment} from './service-env.mjs';

test('public process environments retain connection configuration without execution credentials',()=>{
  const source={PATH:'/bin',LEA_SHARED_DATA_DIR:'/data',LEA_API_BASE_URL:'http://localhost:8001',OPENAI_API_KEY:'provider',CUSTOM_AUTH_TOKEN:'token',GITHUB_TOKEN:'git',PASSWORD:'password',SECRET:'secret'};
  for (const role of ['adapter','web','companion']) {
    assert.deepEqual(serviceEnvironment(role,source),{PATH:'/bin',LEA_SHARED_DATA_DIR:'/data',LEA_API_BASE_URL:'http://localhost:8001'});
  }
  assert.deepEqual(serviceEnvironment('worker',source),source);
  assert.equal(source.OPENAI_API_KEY,'provider','role selection never mutates the parent environment');
});
