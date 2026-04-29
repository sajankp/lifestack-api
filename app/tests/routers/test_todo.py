import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_todo_crud_flow_is_workspace_scoped(client: AsyncClient):
    user_one = {
        "email": "todo@example.com",
        "username": "todouser",
        "password": "todopassword",
    }
    register_res = await client.post("/v1/auth/register", json=user_one)
    assert register_res.status_code == 200

    login_res = await client.post(
        "/v1/auth/login", data={"username": user_one["username"], "password": user_one["password"]}
    )
    assert login_res.status_code == 200

    todo_data = {
        "title": "Test Todo",
        "description": "Test Description",
        "priority": "high",
        "due_date": "2026-12-31T23:59:59Z",
    }
    create_res = await client.post("/v1/todo/", json=todo_data)
    assert create_res.status_code == 201
    todo = create_res.json()
    assert todo["title"] == "Test Todo"
    assert todo["priority"] == "high"
    user_one_todo_id = todo["public_id"]

    list_res = await client.get("/v1/todo/")
    assert list_res.status_code == 200
    list_body = list_res.json()
    assert list_body["total"] == 1
    assert list_body["items"][0]["public_id"] == user_one_todo_id

    get_res = await client.get(f"/v1/todo/{user_one_todo_id}")
    assert get_res.status_code == 200
    assert get_res.json()["public_id"] == user_one_todo_id

    update_data = {"completed": True, "title": "Updated Title"}
    patch_res = await client.patch(f"/v1/todo/{user_one_todo_id}", json=update_data)
    assert patch_res.status_code == 200
    updated_todo = patch_res.json()
    assert updated_todo["completed"] is True
    assert updated_todo["title"] == "Updated Title"

    logout_res = await client.post("/v1/auth/logout")
    assert logout_res.status_code == 200

    user_two = {
        "email": "other@example.com",
        "username": "otheruser",
        "password": "otherpassword",
    }
    register_res = await client.post("/v1/auth/register", json=user_two)
    assert register_res.status_code == 200

    login_res = await client.post(
        "/v1/auth/login", data={"username": user_two["username"], "password": user_two["password"]}
    )
    assert login_res.status_code == 200

    user_two_create_res = await client.post(
        "/v1/todo/",
        json={
            "title": "Second User Todo",
            "description": "Private to second user",
            "priority": "low",
        },
    )
    assert user_two_create_res.status_code == 201
    user_two_todo = user_two_create_res.json()
    assert user_two_todo["public_id"] is not None

    user_two_list_res = await client.get("/v1/todo/")
    assert user_two_list_res.status_code == 200
    user_two_list = user_two_list_res.json()
    assert user_two_list["total"] == 1
    assert user_two_list["items"][0]["public_id"] == user_two_todo["public_id"]

    cross_workspace_get = await client.get(f"/v1/todo/{user_one_todo_id}")
    assert cross_workspace_get.status_code == 404

    cross_workspace_patch = await client.patch(
        f"/v1/todo/{user_one_todo_id}",
        json={"title": "Malicious Update"},
    )
    assert cross_workspace_patch.status_code == 404

    cross_workspace_delete = await client.delete(f"/v1/todo/{user_one_todo_id}")
    assert cross_workspace_delete.status_code == 404

    logout_res = await client.post("/v1/auth/logout")
    assert logout_res.status_code == 200

    login_res = await client.post(
        "/v1/auth/login", data={"username": user_one["username"], "password": user_one["password"]}
    )
    assert login_res.status_code == 200

    user_one_list_again = await client.get("/v1/todo/")
    assert user_one_list_again.status_code == 200
    user_one_list = user_one_list_again.json()
    assert user_one_list["total"] == 1
    assert user_one_list["items"][0]["public_id"] == user_one_todo_id
    assert user_one_list["items"][0]["title"] == "Updated Title"

    cross_workspace_get = await client.get(f"/v1/todo/{user_two_todo['public_id']}")
    assert cross_workspace_get.status_code == 404

    delete_res = await client.delete(f"/v1/todo/{user_one_todo_id}")
    assert delete_res.status_code == 204

    get_again_res = await client.get(f"/v1/todo/{user_one_todo_id}")
    assert get_again_res.status_code == 404
