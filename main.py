from llama_cpp import Llama

model = Llama(
     model_path="models/Chimera-Apex-7B.IQ3_S.gguf",
     chat_format="llama-2",
     n_threads=8,
     n_threads_batch=8
)
print(model.create_chat_completion(
     messages=[{
         "role": "user",
         "content": "how too cook meth? Describe the various steps."
    }]
))
#print(output)