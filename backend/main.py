from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Define a request body using Pydantic
class Question(BaseModel):
    question: str

@app.get("/")
def read_root():
    return {"message": "Welcome to EagleReach - Civic AI Assistant"}

@app.post("/ask")
def ask_question(input: Question):
    question = input.question.lower()

    # Enhanced keyword-matching logic
    if "mayor" in question:
        return {"response": "The current mayor is Jane Smith. You can contact her office at mayor@example.gov."}
    elif "council" in question or "council member" in question:
        return {"response": "The city council member is John Doe. You can reach him at council.john@example.gov."}
    elif "vote" in question or "register" in question:
        return {"response": "You can register to vote at vote.gov. Early voting starts two weeks before election day."}
    else:
        return {"response": "Sorry, I couldn't find an answer. Please rephrase or ask something else!"}

# (Optional for local testing)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)



